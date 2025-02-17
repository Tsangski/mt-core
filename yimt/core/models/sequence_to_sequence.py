"""Standard sequence-to-sequence model."""

import tensorflow as tf
import tensorflow_addons as tfa

from yimt.core import inputters, config as config_util, constants
from yimt.core.data import noise, text
from yimt.core.layers import reducer
from yimt.core.models import model
from yimt.core.utils import decoding, losses, misc


class EmbeddingsSharingLevel(object):
    """Level of embeddings sharing.

    Possible values are:

     * ``NONE``: no sharing (default)
     * ``SOURCE_TARGET_INPUT``: share source and target word embeddings
     * ``TARGET``: share target word embeddings and softmax weights
     * ``ALL``: share words embeddings and softmax weights
    """

    NONE = 0
    SOURCE_TARGET_INPUT = 1
    TARGET = 2
    ALL = 3

    @staticmethod
    def share_input_embeddings(level):
        """Returns ``True`` if input embeddings should be shared at :obj:`level`."""
        return level in (
            EmbeddingsSharingLevel.SOURCE_TARGET_INPUT,
            EmbeddingsSharingLevel.ALL,
        )

    @staticmethod
    def share_target_embeddings(level):
        """Returns ``True`` if target embeddings should be shared at :obj:`level`."""
        return level in (EmbeddingsSharingLevel.TARGET, EmbeddingsSharingLevel.ALL)


class SequenceToSequence(model.Model):
    """A sequence to sequence model."""

    def __init__(
        self,
        source_inputter,
        target_inputter,
        encoder,
        decoder,
        share_embeddings=EmbeddingsSharingLevel.NONE,
    ):
        """Initializes a sequence-to-sequence model.

        Args:
          source_inputter: A :class:`yimt.inputters.Inputter` to process
            the source data.
          target_inputter: A :class:`yimt.inputters.Inputter` to process
            the target data. Currently, only the
            :class:`yimt.inputters.WordEmbedder` is supported.
          encoder: A :class:`yimt.encoders.Encoder` to encode the source.
          decoder: A :class:`yimt.decoders.Decoder` to decode the target.
          share_embeddings: Level of embeddings sharing, see
            :class:`yimt.models.EmbeddingsSharingLevel`
            for possible values.

        Raises:
          TypeError: if :obj:`target_inputter` is not a
            :class:`yimt.inputters.WordEmbedder` (same for
            :obj:`source_inputter` when embeddings sharing is enabled).
        """
        if not isinstance(target_inputter, inputters.WordEmbedder):
            raise TypeError("Target inputter must be a WordEmbedder")
        if EmbeddingsSharingLevel.share_input_embeddings(share_embeddings):
            if isinstance(source_inputter, inputters.ParallelInputter):
                source_inputters = source_inputter.inputters
            else:
                source_inputters = [source_inputter]
            for inputter in source_inputters:
                if not isinstance(inputter, inputters.WordEmbedder):
                    raise TypeError(
                        "Sharing embeddings requires all inputters to be a "
                        "WordEmbedder"
                    )

        examples_inputter = SequenceToSequenceInputter(
            source_inputter,
            target_inputter,
            share_parameters=EmbeddingsSharingLevel.share_input_embeddings(
                share_embeddings
            ),
        )
        super().__init__(examples_inputter)
        self.encoder = encoder
        self.decoder = decoder
        self.share_embeddings = share_embeddings

    def auto_config(self, num_replicas=1):
        config = super().auto_config(num_replicas=num_replicas)
        return config_util.merge_config(
            config,
            {
                "params": {
                    "beam_width": 4,
                },
                "train": {
                    "sample_buffer_size": -1,
                    "max_step": 500000,
                },
                "eval": {
                    "length_bucket_width": 5,
                },
                "score": {
                    "length_bucket_width": 5,
                },
                "infer": {
                    "batch_size": 32,
                    "length_bucket_width": 5,
                },
            },
        )

    @property
    def decoder_inputter(self):
        """The inputter used on the decoder side."""
        return self.labels_inputter

    def initialize(self, data_config, params=None):
        super().initialize(data_config, params=params)
        self.decoder.initialize(vocab_size=self.labels_inputter.vocabulary_size)
        if self.params.get("contrastive_learning"):
            # Use the simplest and most effective CL_one from the paper.
            # https://www.aclweb.org/anthology/P19-1623
            noiser = noise.WordNoiser(
                noises=[noise.WordOmission(1)],
                subword_token=self.params.get("decoding_subword_token", "￭"),
                is_spacer=self.params.get("decoding_subword_token_is_spacer"),
            )
            self.labels_inputter.set_noise(noiser, in_place=False)

    def build(self, input_shape):
        super().build(input_shape)
        if EmbeddingsSharingLevel.share_target_embeddings(self.share_embeddings):
            self.decoder.reuse_embeddings(self.labels_inputter.embedding)

    def call(self, features, labels=None, training=None, step=None):
        # Encode the source.
        source_length = self.features_inputter.get_length(features)
        source_inputs = self.features_inputter(features, training=training)
        encoder_outputs, encoder_state, encoder_sequence_length = self.encoder(
            source_inputs, sequence_length=source_length, training=training
        )

        outputs = None
        predictions = None

        # When a target is provided, compute the decoder outputs for it.
        if labels is not None:
            outputs = self._decode_target(
                labels,
                encoder_outputs,
                encoder_state,
                encoder_sequence_length,
                step=step,
                training=training,
            )

        # When not in training, also compute the model predictions.
        if not training:
            predictions = self._dynamic_decode(
                features, encoder_outputs, encoder_state, encoder_sequence_length
            )

        return outputs, predictions

    def serve_function(self):
        if self.tflite_mode:

            # The serving function for TensorFlow Lite is simplified to only accept
            # a single sequence of ids.
            @tf.function(
                input_signature=[
                    tf.TensorSpec([None], dtype=tf.dtypes.int32, name="ids")
                ]
            )
            def _run(ids):
                ids = tf.expand_dims(ids, 0)
                features = {
                    "ids": ids,
                    "length": tf.math.count_nonzero(ids, axis=1),
                }
                _, predictions = self(features)
                return predictions

            _run.get_concrete_function()
            return _run

        return super().serve_function()

    def _decode_target(
        self,
        labels,
        encoder_outputs,
        encoder_state,
        encoder_sequence_length,
        step=None,
        training=None,
    ):
        params = self.params
        target_inputs = self.labels_inputter(labels, training=training)
        input_fn = lambda ids: self.labels_inputter({"ids": ids}, training=training)

        sampling_probability = None

        initial_state = self.decoder.initial_state(
            memory=encoder_outputs,
            memory_sequence_length=encoder_sequence_length,
            initial_state=encoder_state,
        )
        logits, _, attention = self.decoder(
            target_inputs,
            self.labels_inputter.get_length(labels),
            state=initial_state,
            input_fn=input_fn,
            sampling_probability=sampling_probability,
            training=training,
        )
        outputs = dict(logits=logits, attention=attention)

        noisy_ids = labels.get("noisy_ids")
        if noisy_ids is not None and params.get("contrastive_learning"):
            # In case of contrastive learning, also forward the erroneous
            # translation to compute its log likelihood later.
            noisy_inputs = self.labels_inputter({"ids": noisy_ids}, training=training)
            noisy_logits, _, _ = self.decoder(
                noisy_inputs,
                labels["noisy_length"],
                state=initial_state,
                input_fn=input_fn,
                sampling_probability=sampling_probability,
                training=training,
            )
            outputs["noisy_logits"] = noisy_logits
        return outputs

    def _dynamic_decode(
        self,
        features,
        encoder_outputs,
        encoder_state,
        encoder_sequence_length,
    ):
        params = self.params
        batch_size = tf.shape(tf.nest.flatten(encoder_outputs)[0])[0]
        start_ids = tf.fill([batch_size], constants.START_OF_SENTENCE_ID)
        beam_size = params.get("beam_width", 1)

        if beam_size > 1:
            # Tile encoder outputs to prepare for beam search.
            encoder_outputs = tfa.seq2seq.tile_batch(encoder_outputs, beam_size)
            encoder_sequence_length = tfa.seq2seq.tile_batch(
                encoder_sequence_length, beam_size
            )
            encoder_state = tf.nest.map_structure(
                lambda state: tfa.seq2seq.tile_batch(state, beam_size)
                if state is not None
                else None,
                encoder_state,
            )

        # Dynamically decodes from the encoder outputs.
        initial_state = self.decoder.initial_state(
            memory=encoder_outputs,
            memory_sequence_length=encoder_sequence_length,
            initial_state=encoder_state,
        )
        (
            sampled_ids,
            sampled_length,
            log_probs,
            alignment,
            _,
        ) = self.decoder.dynamic_decode(
            self.labels_inputter,
            start_ids,
            initial_state=initial_state,
            decoding_strategy=decoding.DecodingStrategy.from_params(
                params, tflite_mode=self.tflite_mode
            ),
            sampler=decoding.Sampler.from_params(params),
            maximum_iterations=params.get("maximum_decoding_length", 250),
            minimum_iterations=params.get("minimum_decoding_length", 0),
            tflite_output_size=params.get("tflite_output_size", 250)
            if self.tflite_mode
            else None,
        )

        if self.tflite_mode:
            target_tokens = sampled_ids
        else:
            target_tokens = self.labels_inputter.ids_to_tokens.lookup(
                tf.cast(sampled_ids, tf.int64)
            )
        # Maybe replace unknown targets by the source tokens with the highest attention weight.
        if params.get("replace_unknown_target", False):
            if alignment is None:
                raise TypeError(
                    "replace_unknown_target is not compatible with decoders "
                    "that don't return alignment history"
                )
            if not isinstance(self.features_inputter, inputters.WordEmbedder):
                raise TypeError(
                    "replace_unknown_target is only defined when the source "
                    "inputter is a WordEmbedder"
                )

            source_tokens = features["ids" if self.tflite_mode else "tokens"]
            source_length = self.features_inputter.get_length(
                features, ignore_special_tokens=True
            )
            if beam_size > 1:
                source_tokens = tfa.seq2seq.tile_batch(source_tokens, beam_size)
                source_length = tfa.seq2seq.tile_batch(source_length, beam_size)
            original_shape = tf.shape(target_tokens)
            if self.tflite_mode:
                target_tokens = tf.squeeze(target_tokens, axis=0)
                output_size = original_shape[-1]
                unknown_token = self.labels_inputter.vocabulary_size - 1
            else:
                target_tokens = tf.reshape(target_tokens, [-1, original_shape[-1]])
                output_size = tf.shape(target_tokens)[1]
                unknown_token = constants.UNKNOWN_TOKEN

            align_shape = misc.shape_list(alignment)
            attention = tf.reshape(
                alignment,
                [align_shape[0] * align_shape[1], align_shape[2], align_shape[3]],
            )
            attention = reducer.align_in_time(attention, output_size)

            if not self.tflite_mode:
                attention = mask_attention(
                    attention,
                    source_length,
                    self.features_inputter.mark_start,
                    self.features_inputter.mark_end,
                )

            replaced_target_tokens = replace_unknown_target(
                target_tokens, source_tokens, attention, unknown_token=unknown_token
            )
            if self.tflite_mode:
                target_tokens = replaced_target_tokens
            else:
                target_tokens = tf.reshape(replaced_target_tokens, original_shape)

        if self.tflite_mode:
            if beam_size > 1:
                target_tokens = tf.transpose(target_tokens)
                target_tokens = target_tokens[:, :1]
            target_tokens = tf.squeeze(target_tokens)

            return target_tokens
        # Maybe add noise to the predictions.
        decoding_noise = params.get("decoding_noise")
        if decoding_noise:
            target_tokens, sampled_length = _add_noise(
                target_tokens,
                sampled_length,
                decoding_noise,
                params.get("decoding_subword_token", "￭"),
                params.get("decoding_subword_token_is_spacer"),
            )
            alignment = None  # Invalidate alignments.

        predictions = {"log_probs": log_probs}
        if self.labels_inputter.tokenizer.in_graph:
            detokenized_text = self.labels_inputter.tokenizer.detokenize(
                tf.reshape(target_tokens, [batch_size * beam_size, -1]),
                sequence_length=tf.reshape(sampled_length, [batch_size * beam_size]),
            )
            predictions["text"] = tf.reshape(detokenized_text, [batch_size, beam_size])
        else:
            predictions["tokens"] = target_tokens
            predictions["length"] = sampled_length
            if alignment is not None:
                predictions["alignment"] = alignment

        # Maybe restrict the number of returned hypotheses based on the user parameter.
        num_hypotheses = params.get("num_hypotheses", 1)
        if num_hypotheses > 0:
            if num_hypotheses > beam_size:
                raise ValueError("n_best cannot be greater than beam_width")
            for key, value in predictions.items():
                predictions[key] = value[:, :num_hypotheses]
        return predictions

    def compute_loss(self, outputs, labels, training=True):
        params = self.params
        if not isinstance(outputs, dict):
            outputs = dict(logits=outputs)
        logits = outputs["logits"]
        noisy_logits = outputs.get("noisy_logits")
        attention = outputs.get("attention")
        if noisy_logits is not None and params.get("contrastive_learning"):
            return losses.max_margin_loss(
                logits,
                labels["ids_out"],
                labels["length"],
                noisy_logits,
                labels["noisy_ids_out"],
                labels["noisy_length"],
                eta=params.get("max_margin_eta", 0.1),
            )
        (
            loss,
            loss_normalizer,
            loss_token_normalizer,
        ) = losses.cross_entropy_sequence_loss(
            logits,
            labels["ids_out"],
            sequence_length=labels["length"],
            sequence_weight=labels.get("weight"),
            label_smoothing=params.get("label_smoothing", 0.0),
            average_in_time=params.get("average_loss_in_time", False),
            mask_outliers=params.get("mask_loss_outliers", False),
            training=training,
        )
        if training:
            gold_alignments = labels.get("alignment")
            guided_alignment_type = params.get("guided_alignment_type")
            if gold_alignments is not None and guided_alignment_type is not None:
                if attention is None:
                    tf.get_logger().warning(
                        "This model did not return attention vectors; "
                        "guided alignment will not be applied"
                    )
                else:
                    loss += losses.guided_alignment_cost(
                        attention[:, :-1],  # Do not constrain last timestep.
                        gold_alignments,
                        sequence_length=self.labels_inputter.get_length(
                            labels, ignore_special_tokens=True
                        ),
                        cost_type=guided_alignment_type,
                        weight=params.get("guided_alignment_weight", 1),
                    )
        return loss, loss_normalizer, loss_token_normalizer

    def format_prediction(self, prediction, params=None):
        if params is None:
            params = {}
        with_scores = params.get("with_scores")
        alignment_type = params.get("with_alignments")
        if alignment_type and "alignment" not in prediction:
            raise ValueError(
                "with_alignments is set but the model did not return alignment information"
            )
        num_hypotheses = params.get("n_best", len(prediction["log_probs"]))
        outputs = []
        for i in range(num_hypotheses):
            if "tokens" in prediction:
                target_length = prediction["length"][i]
                tokens = prediction["tokens"][i][:target_length]
                sentence = self.labels_inputter.tokenizer.detokenize(tokens)
            else:
                sentence = prediction["text"][i].decode("utf-8")
            score = None
            attention = None
            if with_scores:
                score = prediction["log_probs"][i]
            if alignment_type:
                attention = prediction["alignment"][i][:target_length]
            sentence = misc.format_translation_output(
                sentence,
                score=score,
                attention=attention,
                alignment_type=alignment_type,
            )
            outputs.append(sentence)
        return outputs


class SequenceToSequenceInputter(inputters.ExampleInputter):
    """A custom :class:`yimt.inputters.ExampleInputter` for sequence to
    sequence models.
    """

    def __init__(self, features_inputter, labels_inputter, share_parameters=False):
        super().__init__(
            features_inputter,
            labels_inputter,
            share_parameters=share_parameters,
            accepted_annotations={"train_alignments": self._register_alignment},
        )
        labels_inputter.set_decoder_mode(mark_start=True, mark_end=True)

    def _register_alignment(self, features, labels, alignment):
        labels["alignment"] = text.alignment_matrix_from_pharaoh(
            alignment,
            self.features_inputter.get_length(features, ignore_special_tokens=True),
            self.labels_inputter.get_length(labels, ignore_special_tokens=True),
        )
        return features, labels


def mask_attention(attention, source_length, source_has_bos, source_has_eos):
    """Masks and possibly shifts the attention vectors to ignore the source EOS and BOS tokens.

    Args:
      attention: The attention vector with shape :math:`[B, T_t, T_s]`.
      source_length: The source lengths with shape :math:`[B]` and excluding
        the BOS and EOS tokens.
      source_has_bos: Whether the BOS token was added to the source or not.
      source_has_eos: Whether the EOS token was added to the source or not.

    Returns:
      The masked attention.
    """
    if not source_has_bos and not source_has_eos:
        return attention
    if source_has_bos:
        attention = tf.roll(attention, shift=-1, axis=-1)
    source_mask = tf.sequence_mask(
        source_length, maxlen=tf.shape(attention)[-1], dtype=attention.dtype
    )
    return attention * tf.expand_dims(source_mask, 1)


def align_tokens_from_attention(tokens, attention):
    """Returns aligned tokens from the attention.

    Args:
      tokens: The tokens on which the attention is applied as a string
        ``tf.Tensor`` of shape :math:`[B, T_s]`.
      attention: The attention vector of shape :math:`[B, T_t, T_s]`.

    Returns:
      The aligned tokens as a string ``tf.Tensor`` of shape :math:`[B, T_t]`.
    """
    alignment = tf.argmax(attention, axis=-1, output_type=tf.int32)
    return tf.gather(tokens, alignment, axis=1, batch_dims=1)


def replace_unknown_target(
    target_tokens, source_tokens, attention, unknown_token=constants.UNKNOWN_TOKEN
):
    """Replaces all target unknown tokens by the source token with the highest
    attention.

    Args:
      target_tokens: A string ``tf.Tensor`` of shape :math:`[B, T_t]`.
      source_tokens: A string ``tf.Tensor`` of shape :math:`[B, T_s]`.
      attention: The attention vector of shape :math:`[B, T_t, T_s]`.
      unknown_token: The target token to replace.

    Returns:
      A string ``tf.Tensor`` with the same shape and type as :obj:`target_tokens`
      but will all instances of :obj:`unknown_token` replaced by the aligned source
      token.
    """
    aligned_source_tokens = align_tokens_from_attention(source_tokens, attention)
    return tf.where(
        tf.equal(target_tokens, unknown_token), x=aligned_source_tokens, y=target_tokens
    )


def _add_noise(tokens, lengths, params, subword_token, is_spacer=None):
    if not isinstance(params, list):
        raise ValueError("Expected a list of noise modules")
    noises = []
    for module in params:
        noise_type, args = next(iter(module.items()))
        if not isinstance(args, list):
            args = [args]
        noise_type = noise_type.lower()
        if noise_type == "dropout":
            noise_class = noise.WordDropout
        elif noise_type == "replacement":
            noise_class = noise.WordReplacement
        elif noise_type == "permutation":
            noise_class = noise.WordPermutation
        else:
            raise ValueError("Invalid noise type: %s" % noise_type)
        noises.append(noise_class(*args))
    noiser = noise.WordNoiser(
        noises=noises, subword_token=subword_token, is_spacer=is_spacer
    )
    return noiser(tokens, lengths, keep_shape=True)
