import torch.nn as nn
import torch.nn.functional as F
import torch
import torch.cuda

import onmt
import onmt.io
from onmt.Utils import aeq


class CopyGenerator(nn.Module):
    """Generator module that additionally considers copying
    words directly from the source.

    The main idea is that we have an extended "dynamic dictionary".
    It contains `|tgt_dict|` words plus an arbitrary number of
    additional words introduced by the source sentence.
    For each source sentence we have a `src_map` that maps
    each source word to an index in `tgt_dict` if it known, or
    else to an extra word.

    The copy generator is an extended version of the standard
    generator that computse three values.

    * :math:`p_{softmax}` the standard softmax over `tgt_dict`
    * :math:`p(z)` the probability of instead copying a
      word from the source, computed using a bernoulli
    * :math:`p_{copy}` the probility of copying a word instead.
      taken from the attention distribution directly.

    The model returns a distribution over the extend dictionary,
    computed as

    :math:`p(w) = p(z=1)  p_{copy}(w)  +  p(z=0)  p_{softmax}(w)`


    .. mermaid::

       graph BT
          A[input]
          S[src_map]
          B[softmax]
          BB[switch]
          C[attn]
          D[copy]
          O[output]
          A --> B
          A --> BB
          S --> D
          C --> D
          D --> O
          B --> O
          BB --> O


    Args:
       input_size (int): size of input representation
       tgt_dict (Vocab): output target dictionary

    """
    def __init__(self, input_size, tgt_dict):
        super(CopyGenerator, self).__init__()
        self.linear = nn.Linear(input_size, len(tgt_dict))
        self.linear_copy = nn.Linear(input_size, 1)
        self.tgt_dict = tgt_dict

    def forward(self, hidden, attn, src_map, align=None, ptrs=None):
        """
        Compute a distribution over the target dictionary
        extended by the dynamic dictionary implied by compying
        source words.

        Args:
           hidden (`FloatTensor`): hidden outputs `[batch*tlen, input_size]`
           attn (`FloatTensor`): attn for each `[batch*tlen, input_size]`
           src_map (`FloatTensor`):
             A sparse indicator matrix mapping each source word to
             its index in the "extended" vocab containing.
             `[src_len, batch, extra_words]`
        """
        # CHECKS
        batch_by_tlen, _ = hidden.size()
        batch_by_tlen_, slen = attn.size()
        slen_, batch, cvocab = src_map.size()
        aeq(batch_by_tlen, batch_by_tlen_)
        aeq(slen, slen_)

        # Original probabilities.
        logits = self.linear(hidden)
        logits[:, self.tgt_dict.stoi[onmt.io.PAD_WORD]] = -float('inf')
        prob = F.softmax(logits)

        # Probability of copying p(z=1) batch.
        p_copy = F.sigmoid(self.linear_copy(hidden))
        # Probibility of not copying: p_{word}(w) * (1 - p(z))
        
        if self.training:
            align_unk = align.eq(0).float().view(-1, 1)
            align_not_unk = align.ne(0).float().view(-1, 1)
            out_prob = torch.mul(prob, align_unk.expand_as(prob))
            mul_attn = torch.mul(attn, align_not_unk.expand_as(attn))
            mul_attn = torch.mul(mul_attn, ptrs.view(-1, slen_).float())
        else:        
            out_prob = torch.mul(prob,  1 - p_copy.expand_as(prob))
            mul_attn = torch.mul(attn, p_copy.expand_as(attn))
        
        copy_prob = torch.bmm(mul_attn.view(-1, batch, slen)
                              .transpose(0, 1),
                              src_map.transpose(0, 1)).transpose(0, 1)
        copy_prob = copy_prob.contiguous().view(-1, cvocab)

        return torch.cat([out_prob, copy_prob], 1), p_copy

class CopyGeneratorCriterion(object):
    def __init__(self, vocab_size, force_copy, pad, eps=1e-20):
        self.force_copy = force_copy
        self.eps = eps
        self.offset = vocab_size
        self.pad = pad

    def __call__(self, scores, align, target):
        # Compute unks in align and target for readability
        align_unk = align.eq(0).float()
        align_not_unk = align.ne(0).float()
        target_unk = target.eq(0).float()
        target_not_unk = target.ne(0).float()

        # Copy probability of tokens in source
        out = scores.gather(1, align.view(-1, 1) + self.offset).view(-1)
        # Set scores for unk to 0 and add eps
        out = out.mul(align_not_unk) + self.eps
        # Get scores for tokens in target
        tmp = scores.gather(1, target.view(-1, 1)).view(-1)

        # Regular prob (no unks and unks that can't be copied)
        if not self.force_copy:
            # Add score for non-unks in target
            out = out + tmp.mul(target_not_unk)
            # Add score for when word is unk in both align and tgt
            out = out + tmp.mul(align_unk).mul(target_unk)
        else:
            # Forced copy. Add only probability for not-copied tokens
            out = out + tmp.mul(align_unk)

        # Drop padding.
        loss = -out.log().mul(target.ne(self.pad).float())
        return loss

class CopyGeneratorLossCompute(onmt.Loss.LossComputeBase):
    """
    Copy Generator Loss Computation.
    """
    def __init__(self, generator, tgt_vocab,
                 force_copy, normalize_by_length,
                 eps=1e-20):
        super(CopyGeneratorLossCompute, self).__init__(
            generator, tgt_vocab)

        # We lazily load datasets when there are more than one, so postpone
        # the setting of cur_dataset.
        self.cur_dataset = None
        self.force_copy = force_copy
        self.normalize_by_length = normalize_by_length
        self.criterion = CopyGeneratorCriterion(len(tgt_vocab), force_copy,
                                                self.padding_idx)
        self.switch_loss_criterion = nn.BCELoss(size_average=False)

    def _make_shard_state(self, batch, output, ref_output, range_, ref_range_, attns, ref_attns):
        """ See base class for args description. """
        if getattr(batch, "alignment", None) is None:
            raise AssertionError("using -copy_attn you need to pass in "
                                 "-dynamic_dict during preprocess stage.")
        return {
            "output": output,
            "ref_output": ref_output,
            "target": batch.tgt2[range_[0] + 1: range_[1]],
            "ref_target": batch.ref_tgt[ref_range_[0] + 1: ref_range_[1]],
            "copy_attn": attns.get("copy"),
            "ref_copy_attn": ref_attns.get("copy"),
            "align": batch.alignment[range_[0] + 1: range_[1]],
            "ref_align": batch.ref_alignment[ref_range_[0] + 1: ref_range_[1]],
            "ptrs": batch.ptrs[range_[0] + 1: range_[1]],
            "ref_ptrs": batch.ref_ptrs[ref_range_[0] + 1: ref_range_[1]]
        }

    def _compute_loss(self, lambda_, batch, output, ref_output, target, ref_target, copy_attn, ref_copy_attn, align, ref_align, ptrs, ref_ptrs):
        """
        Compute the loss. The args must match self._make_shard_state().
        Args:
            batch: the current batch.
            output: the predict output from the model.
            target: the validate target to compare output with.
            copy_attn: the copy attention value.
            align: the align info.
        """
        target = target.view(-1)
        align = align.view(-1)
        scores, p_copy = self.generator(self._bottle(output),
                                self._bottle(copy_attn),
                                batch.src_map, align, ptrs)
        content_loss = self.criterion(scores, align, target)
        content_switch_loss = self.switch_loss_criterion(p_copy, align.ne(0).float().view(-1, 1))
        content_scores_data = scores.data.clone()
        content_scores_data = onmt.io.TextDataset.collapse_copy_scores(
                self._unbottle(content_scores_data, batch.batch_size),
                batch, self.tgt_vocab, self.cur_dataset.src_vocabs)
        content_scores_data = self._bottle(content_scores_data)

        ref_target = ref_target.view(-1)
        ref_align = ref_align.view(-1)
        ref_scores, ref_p_copy = self.generator(self._bottle(ref_output),
                                self._bottle(ref_copy_attn),
                                batch.ref_src_map, ref_align, ref_ptrs)
        style_loss = self.criterion(ref_scores, ref_align, ref_target)
        style_switch_loss = self.switch_loss_criterion(ref_p_copy, ref_align.ne(0).float().view(-1, 1))
        style_scores_data = ref_scores.data.clone()
        style_scores_data = onmt.io.TextDataset.collapse_copy_scores(
                self._unbottle(style_scores_data, batch.batch_size),
                batch, self.tgt_vocab, self.cur_dataset.ref_src_vocabs)
        style_scores_data = self._bottle(style_scores_data)

        # Correct target copy token instead of <unk>
        # tgt[i] = align[i] + len(tgt_vocab)
        # for i such that tgt[i] == 0 and align[i] != 0
        target_data = target.data.clone()
        correct_mask = target_data.eq(0) * align.data.ne(0)
        correct_copy = (align.data + len(self.tgt_vocab)) * correct_mask.long()
        target_data = target_data + correct_copy

        ref_target_data = ref_target.data.clone()
        ref_correct_mask = ref_target_data.eq(0) * ref_align.data.ne(0)
        ref_correct_copy = (ref_align.data + len(self.tgt_vocab)) * ref_correct_mask.long()
        ref_target_data = ref_target_data + ref_correct_copy

        # Compute sum of perplexities for stats
        content_loss_data = content_loss.sum().data.clone()
        content_stats = self._stats(content_loss_data, content_scores_data, target_data)

        style_loss_data = style_loss.sum().data.clone()
        style_stats = self._stats(style_loss_data, style_scores_data, ref_target_data)

        if self.normalize_by_length:
            # Compute Loss as NLL divided by seq length
            # Compute Sequence Lengths
            pad_ix = batch.dataset.fields['tgt2'].vocab.stoi[onmt.io.PAD_WORD]
            tgt_lens = batch.tgt2.ne(pad_ix).sum(0).float()
            ref_tgt_lens = batch.ref_tgt.ne(pad_ix).sum(0).float()
            # Compute Total Loss per sequence in batch
            content_loss = content_loss.view(-1, batch.batch_size).sum(0)
            style_loss = style_loss.view(-1, batch.batch_size).sum(0)
            # Divide by length of each sequence and sum
            content_loss = torch.div(content_loss, tgt_lens).sum()
            style_loss = torch.div(style_loss, ref_tgt_lens).sum()
        else:
            content_loss = content_loss.sum()
            style_loss = style_loss.sum()

        content_loss = content_loss + content_switch_loss
        style_loss = style_loss + style_switch_loss
        
        loss = lambda_ * content_loss + (1 - lambda_) * style_loss
        
        return loss, content_stats, style_stats
