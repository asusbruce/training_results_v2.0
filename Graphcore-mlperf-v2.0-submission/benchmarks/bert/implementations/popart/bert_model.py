# Copyright (c) 2019 Graphcore Ltd. All rights reserved.

import popart
import numpy as np
from scipy.stats import truncnorm
from typing import NamedTuple, Optional
from contextlib import ExitStack
from collections import defaultdict
from enum import Enum
import logging
import math
import os
from collections import defaultdict
from contextlib import ExitStack, contextmanager
from enum import Enum
from functools import reduce
from itertools import chain
from typing import List, NamedTuple, Optional

import numpy as np
from scipy.stats import truncnorm

import popart

logger = logging.getLogger(__name__)


class ExecutionMode(str, Enum):
    DEFAULT = "DEFAULT"
    PIPELINE = "PIPELINE"


class BertConfig(NamedTuple):
    batch_size: int = 1
    sequence_length: int = 128
    max_positional_length: int = 512
    max_sequences_per_pack: int = 1

    # Look up embedding on CPU
    # Possible values:
    #   NONE  = all embeddings on IPU
    #   WORD  = word embeddings on CPU, position embeddings on IPU
    #   ALL   = all embeddings on CPU, both word and position embeddings sent to IPU
    #   MERGE = all embeddings on CPU, sum of word and position embeddings sent to IPU
    host_embedding: str = "NONE"
    # PRETRAINING Only
    mask_tokens: int = 20

    vocab_length: int = 30912
    hidden_size: int = 768

    # Feed Forward is 4 * hidden_size unless specified by --ff-size
    ff_size__: Optional[int] = None

    @property
    def ff_size(self):
        if self.ff_size__ is not None:
            return self.ff_size__
        return self.hidden_size * 4

    @property
    def padded_vocab_length(self):
        '''Computes padding needed to make vocab length divisible by serialization steps'''
        splits = self.embedding_serialization_vocab_steps
        length = self.vocab_length
        padding = (splits - length % splits) % splits
        return length + padding

    attention_heads: int = 12

    inference: bool = False

    num_layers: int = 2

    # Specify which IPU to place individual attention and FFN blocks onto
    # For example, 2 att and 1 ffn on IPU1 followed by 1 att and 2 ffn on IPU0
    # can be specified as: [ [1, [2, 1]], [0, [1, 2]] ]
    att_ffn_placement: List[List] = []

    # Specify the pipeline stage on which to start the encoder
    encoder_start_pipeline_stage: int = 1

    # Specify the available memory proportion to be used by the Encoder MatMuls.
    # this can be a single element list or a
    # list providing a specific value for each IPU.
    available_memory_proportion: List[float] = [0.1525878906]

    split_transformer: bool = False

    no_dropout: bool = False
    no_attn_dropout: bool = False
    dropout_prob: float = 0.1
    attn_dropout_prob: float = 0.1

    reduce_nsp_overhead: bool = False

    layer_norm_eps: float = 0.001

    # Choices: PRETRAINING (MLM + NSP), SQUAD
    task: str = "PRETRAINING"

    # This option serializes all matmul layers to multiples
    # {N, hidden_size} x {hidden_size, hidden_size}.
    # This is required for sequence length 384.
    split_linear_layers: bool = False

    split_qkv: bool = False

    no_mask: bool = False

    activation_type: str = 'Gelu'

    relu_leak: float = 0.1

    # Choices: FLOAT, FLOAT16
    popart_dtype: str = "FLOAT16"

    lamb_accl_dtype: str = "FLOAT"

    activation_checkpoint_dtype: str = "FLOAT16"

    loss_scaling: float = 1.0

    @property
    def dtype(self):
        if self.popart_dtype == "FLOAT":
            return np.float32
        elif self.popart_dtype == "FLOAT16":
            return np.float16
        else:
            raise ValueError("BertConfig.dtype must be 'FLOAT' or 'FLOAT16'")

    @property
    def max_lm_predictions(self):
        return self.mask_tokens + self.max_sequences_per_pack - 1

    @property
    def qkv_length(self):
        return self.hidden_size / self.attention_heads

    # In PRETRAINING this sets how many steps to serialise both the
    # embedding and projection
    embedding_serialization_vocab_steps: int = 1

    update_embedding_dict: bool = True

    use_default_available_memory_proportion: bool = False

    no_cls_layer: bool = False

    projection_bias: bool = False

    num_attention_splits: int = 1
    num_ffwd_splits: int = 1

    execution_mode: ExecutionMode = ExecutionMode.DEFAULT
    num_io_tiles: int = 0


class DeviceScope(object):
    def __init__(self,
                 builder,
                 execution_mode=ExecutionMode.DEFAULT,
                 virtualGraph=None,
                 pipelineStage=None,
                 nameScope=None,
                 additional_scopes=None):
        self.builder = builder
        self.execution_mode = execution_mode
        self.virtualGraph = virtualGraph
        self.pipelineStage = pipelineStage
        self.nameScope = nameScope
        self.additional_scopes = additional_scopes or []

    def __enter__(self):
        self.stack = ExitStack()
        if self.virtualGraph is not None:
            self.stack.enter_context(self.builder.virtualGraph(self.virtualGraph))

        if self.execution_mode == ExecutionMode.PIPELINE\
                and self.pipelineStage is not None:
            self.stack.enter_context(
                self.builder.pipelineStage(self.pipelineStage))

        if self.nameScope is not None:
            self.stack.enter_context(self.builder.nameScope(self.nameScope))
        for scope in self.additional_scopes:
            self.stack.enter_context(scope)
        return self

    def __exit__(self, *exp):
        self.stack.close()
        return False


class Model(object):
    def __init__(self, builder=popart.Builder(), initializers=None, execution_mode=ExecutionMode.DEFAULT):
        if initializers is None:
            initializers = {}
        self.builder = builder
        self.initializers = initializers
        if type(execution_mode) == str:
            execution_mode = ExecutionMode(execution_mode)
        self.execution_mode = execution_mode

        # Keep track of tensors in order to give them different parameters
        self.tensors = defaultdict(list)

    def normal_init_tensor(self, dtype, shape, mean, std_dev, debug_name=""):
        data = self.normal_init_data(dtype, shape, mean, std_dev, debug_name)
        tensor = self.builder.addInitializedInputTensor(data, debug_name)
        self._add_to_tensor_map(tensor)
        return tensor

    def normal_init_data(self, dtype, shape, mean, std_dev, debug_name=""):
        name = self.builder.getNameScope(debug_name)
        data = self.initializers.get(name, None)
        if data is None:
            # Truncated random normal between 2 standard devations
            data = truncnorm.rvs(-2, 2, loc=mean,
                                 scale=std_dev, size=np.prod(shape))
            data = data.reshape(shape).astype(dtype)
            self.initializers[name] = data
        else:
            if np.any(data.shape != np.array(shape)):
                if np.all(data.T.shape == np.array(shape)):
                    data = data.T.copy()
                    logger.warning(
                        f"Initializer for {name} was provided transposed.")
                else:
                    raise RuntimeError(f"Initializer {name} does not match shapes. \n"
                                       f" Provided {data.shape}. Required {shape}")
        return data

    def constant_init_tensor(self, dtype, shape, scalar, debug_name="", is_const=False):
        data = self.initializers.get(
            self.builder.getNameScope(debug_name), None)
        if data is None:
            data = np.full(shape, scalar).astype(dtype)
        else:
            if np.any(data.shape != shape):
                raise RuntimeError(f"Initializer {self.builder.getNameScope(debug_name)} does not match shapes. \n"
                                   f" Provided {data.shape}. Required {shape}")
        if is_const:
            return self.builder.aiOnnx.constant(data, debug_name)
        tensor = self.builder.addInitializedInputTensor(data, debug_name)
        self._add_to_tensor_map(tensor)
        return tensor

    def constant_tensor(self, value, dtype=None, debug_name=""):
        value = np.array(value)
        if dtype is not None:
            value = value.astype(dtype)
        return self.builder.aiOnnx.constant(value, debug_name)

    def device_scope(self, virtualGraph=None, pipelineStage=None, nameScope=None, additional_scopes=None):
        return DeviceScope(self.builder, self.execution_mode, virtualGraph, pipelineStage, nameScope, additional_scopes)

    def _add_to_tensor_map(self, tensor):
        if self.builder.hasPipelineStage():
            pipeline_stage = self.builder.getPipelineStage()
            self.tensors[pipeline_stage].append(tensor)
        else:
            self.tensors[0].append(tensor)


class Bert(Model):
    def __init__(self, config, **kwargs):
        super().__init__(**kwargs)
        self.config = config

        self.dropout_modifier = 256

        # This is the TensorId for the shared embedding & projection
        self.embedding_dict = None

        # This dict[ipu,TensorId] reuses any mask already generated on an IPU/pipeline stage
        self.reusable_masks = {}

        self.init_device_placement()

    def init_device_placement(self):
        ''' Create a DeviceScope for each Embedding, SQUAD, NSP
            and also determine the pipeline stage and IPU placement for layer
        '''

        # Embedding
        self.embedding_scope = self.device_scope(0, 0)

        # Task Layers
        final_pipeline_stage = self.config.encoder_start_pipeline_stage + len(self.config.att_ffn_placement) - 1

        # To handle configurations where the last layer is not on IPU 0
        # and we need an new pipeline stage for the MLM and loss
        final_ipu = self.config.att_ffn_placement[-1][0]
        if final_ipu != 0:
            final_pipeline_stage += 1

        if self.config.task in ("NSP", "PRETRAINING"):
            self.nsp_scope = self.device_scope(0, final_pipeline_stage, "NSP")
            self.cls_scope = self.device_scope(0, final_pipeline_stage, "CLS")
        if self.config.task == "PRETRAINING":
            self.mlm_scope = self.device_scope(0, final_pipeline_stage, "MLM")
            self.final_loss_scope = self.mlm_scope
        if self.config.task == "SQUAD":
            self.squad_scope = self.device_scope(0, final_pipeline_stage, "Squad")
            self.final_loss_scope = self.squad_scope

        # Scope to place all IO on first IPU for inference:
        if self.config.inference:
            self.output_scope = self.device_scope(0, final_pipeline_stage, "Output")
        else:
            self.output_scope = None

        self.total_pipeline_stages = final_pipeline_stage

        # For each layer determine which ipu it should be placed on
        p_start = self.config.encoder_start_pipeline_stage
        self.placement = {}
        self.placement["att_device"] = list(chain(*[[i]*num_att for i, (num_att, _) in self.config.att_ffn_placement]))
        self.placement["ffn_device"] = list(chain(*[[i]*num_ffn for i, (_, num_ffn) in self.config.att_ffn_placement]))
        self.placement["att_stage"] = list(chain(*[[p_start + p]*num_att for p, (_, (num_att, _)) in enumerate(self.config.att_ffn_placement)]))
        self.placement["ffn_stage"] = list(chain(*[[p_start + p]*num_ffn for p, (_, (_, num_ffn)) in enumerate(self.config.att_ffn_placement)]))
        logger.debug(f"Layer placement: {self.placement}")

    @property
    def total_ipus(self):
        return max(map(lambda x: x[0], self.config.att_ffn_placement)) + 1

    def should_checkpoint_att(self, att_index):
        '''Checkpoint the output of attention layers on the last stage'''
        if self.placement["att_stage"][att_index] == self.total_pipeline_stages:
            return True
        return False

    def should_checkpoint_ffn(self, ffn_index):
        '''Checkpoint the output of ffn layers which are not to be copied to the next pipelineStage'''

        # If this is the last layer to be placed on an IPU then we avoid checkpointing it to save memory
        # The output of the very last FFN in the model is also checkpointed (does not affect liveness)
        if ffn_index == self.config.num_layers - 1:
            return True

        # Check if there are further FFN stages on this IPU
        last_ffn_on_this_ipu = self.placement["ffn_device"][ffn_index] != self.placement["ffn_device"][ffn_index+1]
        # There could still be another attention layer after this FFN when splitting layers
        last_but_followed_by_att = self.placement["ffn_device"][ffn_index] == self.placement["att_device"][ffn_index+1]
        if not last_ffn_on_this_ipu or last_but_followed_by_att:
            return True


    def set_available_memory_proportion(self, x):
        if self.config.use_default_available_memory_proportion:
            return x

        if len(self.config.available_memory_proportion) == 1:
            amp = self.config.available_memory_proportion[0]
        else:
            vgraph = self.builder.getVirtualGraph()
            amp = self.config.available_memory_proportion[vgraph]

        self.builder.setAvailableMemoryProportion(x, amp)
        return x

    def checkpoint(self, x, is_last_stage):
        # The checkpoint of the last stage is not pushed into stash
        # not need to convert it to FP8
        if not is_last_stage and self.config.activation_checkpoint_dtype == "FLOAT8":
            x = self.builder.customOp(opName="CastToFp8",
                                      opVersion=1,
                                      domain="ai.graphcore",
                                      inputs=[x],
                                      attributes={"nBitMantissa": "4",
                                                  "nBitExponent": "3",
                                                  "exponentBias": "0"})[0]

            x = self.builder.checkpointOutput([x])[0]

            x = self.builder.customOp(opName="CastFromFp8",
                                      opVersion=1,
                                      domain="ai.graphcore",
                                      inputs=[x],
                                      attributes={"nBitMantissa": "4",
                                                  "nBitExponent": "3",
                                                  "exponentBias": "0",
                                                  "to": self.config.popart_dtype})[0]
        else:
            x = self.builder.checkpointOutput([x])[0]

        return x

    def build_graph(self):
        indices = self.inputs["input_ids"]
        positions = self.inputs["position_ids"]
        segments = self.inputs["segment_ids"]
        masks = self.masks if len(self.masks) > 0 else None

        # Embedding
        with self.builder.nameScope("Embedding"):
            x = self.embedding(indices, positions, segments)

        # Stream the mask on to the first IPU that carries an encoder layer
        if masks is not None:
            ipu = self.placement["att_device"][0]
            stage = self.placement["att_stage"][0]
            with self.device_scope(ipu, stage, "Layer0"):
                masks = [self.detach(mask) for mask in masks]

        for i in range(self.config.num_layers):
            ipu = self.placement["att_device"][i]
            stage = self.placement["att_stage"][i]
            with self.device_scope(ipu, stage, f"Layer{i}"):
                with self.builder.nameScope("Attention"):
                    x = self.attention(x, masks)

                if self.should_checkpoint_att(i):
                    logger.debug(f"Inserting checkpoint for attention output of layer {i}.")
                    x = self.checkpoint(x, stage == self.total_pipeline_stages)

            ipu = self.placement["ffn_device"][i]
            stage = self.placement["ffn_stage"][i]
            with self.device_scope(ipu, stage, f"Layer{i}"):
                with self.builder.nameScope("FF"):
                    x = self.feed_forward(x)

                if self.should_checkpoint_ffn(i):
                    logger.debug(f"Inserting checkpoint for feed forward output of layer {i}.")
                    x = self.checkpoint(x, stage == self.total_pipeline_stages)

        outputs = []
        # PreTraining tasks
        if self.config.task in ("NSP", "PRETRAINING") and not self.config.reduce_nsp_overhead:
            with self.nsp_scope:
                outputs.append(self.nsp_head(x))

        if self.config.task == "PRETRAINING":
            with self.cls_scope:
                if self.config.no_cls_layer:
                    predictions = self.builder.aiOnnx.identity([x])
                else:
                    predictions = self.lm_prediction_head(x)
            with self.mlm_scope:
                outputs = [self.projection(predictions)] + outputs

        # Fine Tuning tasks
        if self.config.task == "SQUAD":
            with self.squad_scope:
                squad_outputs = self.squad_projection(x)

            if self.output_scope:
                with self.output_scope:
                    outputs += [self.detach(tensor)
                                for tensor in squad_outputs]
            else:
                outputs += squad_outputs

        if self.config.task == "MRPC":
            # TODO: Implement this: T11026
            raise NotImplementedError()

        return tuple(outputs)

    def norm(self, input_x):
        gamma = self.constant_init_tensor(
            self.config.dtype, (self.config.hidden_size,), 1, "Gamma")
        beta = self.constant_init_tensor(
            self.config.dtype, (self.config.hidden_size,), 0, "Beta")

        outs = self.builder.aiGraphcore.groupnormalization(
            [input_x, gamma, beta], 1, self.config.layer_norm_eps)
        return outs[0]

    def dropout_with_training_switch(self, input_x, is_training):
        output = self.builder.customOp(opName="DropoutWithTrainingSwitch",
                                       opVersion=1,
                                       domain="ai.graphcore",
                                       inputs=[input_x, is_training],
                                       attributes={"ratio": self.config.dropout_prob})[0]
        return output

    def dropout(self, input_x):
        if not self.config.no_dropout:
            is_training = self.inputs["is_training"]
            input_x = self.dropout_with_training_switch(input_x, is_training)
        return input_x

    def leaky_relu(self, input_x, alpha):
        """
            This function implements the leaky relu activation function.
            The mathematical function is:
            Leaky_Relu(x) = Relu(x) - alpha*Relu(-x)
        """
        alpha_t = self.builder.aiOnnx.constant(
            np.asarray([alpha], dtype=self.config.dtype)
        )
        result_plus = self.builder.aiOnnx.relu([input_x])
        minus_x = self.builder.aiOnnx.neg([input_x])
        result_minus = self.builder.aiOnnx.relu([minus_x])
        result_minus = self.builder.aiOnnx.mul([alpha_t, result_minus])
        result = self.builder.aiOnnx.sub([result_plus, result_minus])
        return result

    def simplified_gelu(self, input_x):
        """
            Simpler implementation of the GELU based on the sigmoid.
            Coming from the original Gelu paper (https://arxiv.org/abs/1606.08415).
        """
        scale = self.builder.aiOnnx.constant(
            np.asarray([1.702], dtype=self.config.dtype))
        result = self.builder.aiOnnx.mul([scale, input_x])
        result = self.builder.aiOnnx.sigmoid([result])
        result = self.builder.aiOnnx.mul([input_x, result])
        return result

    def intermediate_activation_function(self, input_x):
        if self.config.activation_type == 'Gelu':
            return self.builder.aiGraphcore.gelu([input_x])
        elif self.config.activation_type == 'SGelu':
            return self.simplified_gelu(input_x)
        elif self.config.activation_type == 'LRelu':
            return self.leaky_relu(input_x, alpha=self.config.relu_leak)
        else:
            return self.builder.aiOnnx.relu([input_x])

    def feed_forward(self, input_x):
        # If using `split_linear_layers` num_splits should make each matmul of size [hidden, hidden]
        num_splits = self.config.ff_size // self.config.hidden_size
        with self.builder.nameScope("1"):
            weight1 = self.normal_init_tensor(self.config.dtype,
                                              [self.config.hidden_size,
                                               self.config.ff_size],
                                              0, 0.02,
                                              "W")
            bias1 = self.constant_init_tensor(self.config.dtype,
                                              (self.config.ff_size,),
                                              0,
                                              "B")
            x = self.builder.aiOnnx.matmul([input_x, weight1])
            self.set_available_memory_proportion(x)
            if self.config.split_linear_layers:
                self.builder.setSerializeMatMul({x},
                                                'output_channels',
                                                num_splits,
                                                keep_precision=True)
            x = self.builder.aiOnnx.add([x, bias1])

        x = self.intermediate_activation_function(x)

        with self.builder.nameScope("2"):
            weight2 = self.normal_init_tensor(self.config.dtype,
                                              [self.config.ff_size,
                                               self.config.hidden_size],
                                              0, 0.02,
                                              "W")
            bias2 = self.constant_init_tensor(self.config.dtype,
                                              (self.config.hidden_size,),
                                              0,
                                              "B")
            x = self.builder.aiOnnx.matmul([x, weight2])
            self.set_available_memory_proportion(x)
            if self.config.split_linear_layers:
                self.builder.setSerializeMatMul({x},
                                                'reducing_dim',
                                                num_splits,
                                                keep_precision=True)
            x = self.builder.aiOnnx.add([x, bias2])

        # google-research/bert puts dropout here
        x = self.dropout(x)
        x = self.builder.aiOnnx.add([input_x, x])
        x = self.norm(x)
        return x

    def detach(self, input_x, pass_through_creation=1):
        if self.config.inference:
            return input_x
        return self.builder.customOp(opName="Detach",
                                     opVersion=1,
                                     domain="ai.graphcore",
                                     inputs=[input_x],
                                     attributes={
                                         "pass_through_creation": pass_through_creation
                                     })[0]

    def generate_simplified_periodic_pos_data(self, dtype, shape, scale=4):
        def value(x, y):
            return .02/.707*np.cos(2*scale*np.pi*x*y/shape[1])
        X, Y = np.mgrid[:shape[0], :shape[1]]
        return np.vectorize(value)(X, Y,).astype(dtype)

    def generate_transformer_periodic_pos_data(self, dtype, shape, min_timescale=1.0, max_timescale=1.0e4):
        """
        Periodic position initialiser, from 3.5 of "Attention is All You Need". Adapted from:
        https://github.com/tensorflow/models/tree/master/official/transformer/v2
        """
        position = np.arange(0, shape[0], dtype=dtype)
        num_timescales = shape[1] // 2
        log_timescale_increment = (
            math.log(float(max_timescale) / float(min_timescale)) / (num_timescales - 1))

        hidden_idx = np.arange(0, num_timescales, dtype=dtype)
        inv_timescales = min_timescale * np.exp(
            hidden_idx * -log_timescale_increment)

        expanded_pos = np.expand_dims(position, 1)
        expanded_ts = np.expand_dims(inv_timescales, 0)
        scaled_time = expanded_pos * expanded_ts

        signal = np.concatenate(
            [np.sin(scaled_time), np.cos(scaled_time)], axis=1)
        return signal

    def get_model_embeddings(self):
        embedding_dict = None
        positional_dict = None
        if self.config.host_embedding in ("ALL", "WORD", "MERGE"):
            with self.builder.nameScope("Embedding"):
                embedding_dict = self.normal_init_data(self.config.dtype,
                                                       (self.config.padded_vocab_length, self.config.hidden_size),
                                                       0, 0.02,
                                                       "Embedding_Dict")
                if self.config.host_embedding in ("ALL", "MERGE"):
                    positional_dict = self.normal_init_data(self.config.dtype,
                                                            (self.config.max_positional_length, self.config.hidden_size),
                                                            0, 0.02,
                                                            "Positional_Dict")
        return embedding_dict, positional_dict

    def _split_word_embedding_initializer(self):
        def get_split(idx, full_t):
            num_splits = self.config.embedding_serialization_vocab_steps
            vocab_axis = full_t.shape.index(self.config.padded_vocab_length)
            return np.split(full_t, num_splits, axis=vocab_axis)[idx]

        num_splits = self.config.embedding_serialization_vocab_steps
        embedding_dict = self.initializers["Embedding/Embedding_Dict"]

        embedding_dict_split = {}
        r1 = popart.reservedAccl1Prefix() + "Embedding/Embedding_Dict"
        r2 = popart.reservedAccl2Prefix() + "Embedding/Embedding_Dict"
        for i in range(num_splits):
            embedding_dict_split[f"Embedding/Embedding_Dict/split{i}"] = get_split(i, embedding_dict)

            if r1 in self.initializers:
                lamb1 = self.initializers[r1]
                embedding_dict_split[r1 + f"/split{i}"] = get_split(i, lamb1)

            if r2 in self.initializers:
                lamb2 = self.initializers[r2]
                embedding_dict_split[r2 + f"/split{i}"] = get_split(i, lamb2)

        self.initializers.update(embedding_dict_split)
        del self.initializers["Embedding/Embedding_Dict"]
        if r1 in self.initializers:
            del self.initializers[r1]
        if r2 in self.initializers:
            del self.initializers[r2]

    def embedding(self, indices, positions, segments):

        with self.embedding_scope:
            x = self.gather(indices, self.config.padded_vocab_length, "Embedding_Dict")

            if self.config.task == "PRETRAINING":
                zero = self.constant_tensor([0], np.uint32, "zero")
                positions_mask = self.builder.aiOnnx.greater([self.masks[0], zero])
                positions_mask = self.builder.reshape_const(self.builder.aiOnnx, [positions_mask],
                                                            [self.config.batch_size*self.config.sequence_length, 1])

            else:
                base_value = np.expand_dims(np.arange(self.config.sequence_length), -1)
                base = self.constant_tensor(base_value, np.uint32, "position_mask")
                positions_mask = self.builder.aiOnnx.less([base, self.masks[0]])

            positions_mask = self.builder.aiOnnx.cast([positions_mask], self.config.popart_dtype)
            positions_mask = self.detach(positions_mask)

        with self.embedding_scope:

            segments_onehot = self.builder.aiOnnx.onehot([
                segments,
                self.constant_tensor(2, dtype=np.int32),
                self.constant_tensor([0, 1], dtype=self.config.dtype)])
            segments_weights = self.normal_init_tensor(
                self.config.dtype,
                [2, self.config.hidden_size],
                0, 0.02, "Segment_Dict")
            x_seg = self.builder.aiOnnx.matmul(
                [segments_onehot, segments_weights])

            if self.config.host_embedding != "MERGE":
                x_pos = self.gather(positions,
                                    self.config.max_positional_length,
                                    "Positional_Dict")
                x = self.builder.aiOnnx.add([x, x_pos])
            x = self.builder.aiOnnx.add([x, x_seg])

            # When outlining is enabled, under certain situations, the `add` above resolves
            # to an AddLhsInPlace, which then causes the output to be laid out incorrectly
            # for SQuAD. This workaround ensures it stays as an AddRhsInPlace.
            self.builder.setInplacePreferences(x, {"AddRhsInplace": 1000.0})

            x = self.builder.aiOnnx.mul([x, positions_mask])
            x = self.norm(x)
            x = self.dropout(x)
        return x

    def gather(self, indices, embedding_size, name):
        if self.config.host_embedding in ("ALL", "WORD", "MERGE") and name == "Embedding_Dict":
            return indices
        if self.config.host_embedding in ("ALL", "MERGE") and name == "Positional_Dict":
            return indices
        if name.startswith("Embedding_Dict") and self.config.task == "PRETRAINING":
            # Important that the tied gather/matmul weight with transpose before the gather.
            # This will ensure it matches the custom_ops/tied_gather_pattern.
            embedding_dict = self.normal_init_tensor(
                self.config.dtype,
                (self.config.hidden_size, embedding_size),
                0, 0.02,
                name)
            self.embedding_dict = embedding_dict

            if self.config.inference:
                embedding_dict = self.builder.customOp(opName="PreventConstFolding",
                                                       opVersion=1,
                                                       domain="ai.graphcore",
                                                       inputs=[embedding_dict],
                                                       attributes={})[0]
            embedding_dict = self.builder.aiOnnx.transpose([embedding_dict])
        else:
            embedding_dict = self.normal_init_tensor(
                self.config.dtype,
                (embedding_size, self.config.hidden_size),
                0, 0.02,
                name)

        x = self.builder.aiOnnx.gather([embedding_dict, indices])

        if name == "Embedding_Dict" and not self.config.update_embedding_dict:
            x = self.detach(x)
        return x

    def qkv_weights(self):
        if self.config.split_qkv:
            weights = []
            initializers_to_delete = set()

            for idx, name in enumerate("QKV"):
                for prefix in ["", popart.reservedAccl1Prefix(), popart.reservedAccl2Prefix()]:
                    unsplit_name = prefix + self.builder.getNameScope("QKV")
                    long_name = prefix + self.builder.getNameScope(name)
                    full_t = self.initializers.get(unsplit_name, None)
                    if full_t is not None:
                        initializers_to_delete.add(unsplit_name)
                        self.initializers[long_name] = np.split(full_t, 3, axis=1)[idx]

                # Create the weights from the split initializers
                weights.append(self.normal_init_tensor(
                    self.config.dtype,
                    [self.config.hidden_size, self.config.hidden_size],
                    0, 0.02,
                    name))

            # Clean up the un-split initializers
            for t in initializers_to_delete:
                if t in self.initializers:
                    del self.initializers[t]
            qkv = self.builder.aiOnnx.concat(weights, axis=1)
        else:
            qkv = self.normal_init_tensor(
                self.config.dtype,
                [self.config.hidden_size, 3 * self.config.hidden_size],
                0, 0.02,
                "QKV")
        return qkv

    def attention(self, input_x, masks=None):
        qkv_weights = self.qkv_weights()
        qkv = self.builder.aiOnnx.matmul([input_x, qkv_weights])
        self.set_available_memory_proportion(qkv)
        if self.config.split_linear_layers:
            self.builder.setSerializeMatMul({qkv}, 'output_channels', 3, True)

        # Add attention bias
        qkv_biases = self.constant_init_tensor(
            self.config.dtype,
            (self.config.hidden_size*3,),
            0, "QKV_bias")
        qkv = self.builder.aiOnnx.add([qkv, qkv_biases])

        x = self.attention_onnx(qkv, masks)

        projection_weights = self.normal_init_tensor(
            self.config.dtype,
            [self.config.hidden_size, self.config.hidden_size],
            0, 0.02,
            "Out")

        x = self.builder.aiOnnx.matmul([x, projection_weights])
        self.set_available_memory_proportion(x)

        # Add output biases
        projection_bias = self.constant_init_tensor(
            self.config.dtype,
            (self.config.hidden_size,),
            0, "Out_bias")
        x = self.builder.aiOnnx.add([x, projection_bias])

        x = self.dropout(x)
        x = self.builder.aiOnnx.add([input_x, x])
        x = self.norm(x)
        return x

    def attention_onnx(self, qkv, masks):
        comb_shape = [self.config.batch_size, self.config.sequence_length,
                      self.config.attention_heads, self.config.qkv_length]

        if isinstance(qkv, list):
            split_qkv = qkv
        else:
            split_qkv = self.builder.aiOnnx.split(
                [qkv],
                num_outputs=3,
                axis=1,
                split=[self.config.hidden_size]*3,
                debugContext="QKV_Split")

        def extract_heads(tensor, transpose=False):
            tensor = self.builder.reshape_const(
                self.builder.aiOnnx, [tensor], comb_shape)
            perm = [0, 2, 1, 3] if not transpose else [0, 2, 3, 1]
            return self.builder.aiOnnx.transpose([tensor], perm=perm)

        q, kt, v = [extract_heads(t, i == 1) for i, t in enumerate(split_qkv)]

        # Attention calculation
        with self.builder.nameScope('Z'):
            x = self.builder.aiOnnx.matmul([q, kt])
            self.set_available_memory_proportion(x)

            c = self.constant_tensor(
                1 / np.sqrt(self.config.qkv_length), self.config.dtype)
            x = self.builder.aiOnnx.mul([x, c])

            if not self.config.no_mask or masks is not None:
                final_mask = self.builder.customOp(opName="AttentionMask",
                                                   opVersion=1,
                                                   domain="ai.graphcore",
                                                   inputs=[masks[0], x],
                                                   attributes={"dataType": self.config.popart_dtype})[0]
                final_mask = self.detach(final_mask)
                x = self.builder.aiOnnx.add([x, final_mask], "ApplyMask")

            x = self.builder.aiOnnx.softmax([x], axis=-1)

            if not self.config.no_attn_dropout:
                x = self.dropout(x)

            # x[batch_size, attention_heads, sequence_length, sequence_length] * v[batch_size, attention_heads, sequence_length, qkv_length]
            z = self.builder.aiOnnx.matmul([x, v])
            self.set_available_memory_proportion(z)

            # [batch_size, attention_heads, sequence_length, qkv_length] -> [batch_size, sequence_length, attention_heads, qkv_length]
            z = self.builder.aiOnnx.transpose([z], perm=[0, 2, 1, 3])
            # [batch_size, sequence_length, attention_heads, qkv_length] -> [batch_size*sequence_length, attention_heads*qkv_length]
            z = self.builder.reshape_const(self.builder.aiOnnx, [z], [
                                           self.config.sequence_length * self.config.batch_size, self.config.hidden_size])
        return z

    def projection(self, input_x):
        # MLM tokens have been pre-arranged to the front of the sequence
        x = self.builder.reshape_const(self.builder.aiOnnx, [input_x], [
                                       self.config.batch_size,
                                       self.config.sequence_length,
                                       self.config.hidden_size])

        x = self.builder.aiOnnxOpset9.slice([x], axes=[1], starts=[0], ends=[self.config.max_lm_predictions])

        x = self.builder.reshape_const(self.builder.aiOnnx, [x], [
                                       self.config.batch_size * self.config.max_lm_predictions,
                                       self.config.hidden_size])
        x = self.checkpoint(x, True)

        weight = self.embedding_dict

        # Move the weight to the current pipeline stage
        if weight in self.tensors[self.embedding_scope.pipelineStage]:
            embedding_stage = self.embedding_scope.pipelineStage
            self.tensors[embedding_stage].remove(weight)
            self._add_to_tensor_map(weight)

        x = self.builder.aiOnnx.matmul([x, weight])
        num_splits = self.config.embedding_serialization_vocab_steps
        self.builder.setSerializeMatMul(
            {x}, 'output_channels', num_splits, True)
        x = self.checkpoint(x, True)

        if self.config.projection_bias:
            bias = self.constant_init_tensor(self.config.dtype, (self.config.padded_vocab_length,), 0, "ProjectionB")
            x = self.builder.aiOnnx.add([x, bias])
        x = self.checkpoint(x, True)

        x = self.builder.reshape_const(self.builder.aiOnnx, [x], [
                                       self.config.batch_size, self.config.max_lm_predictions, self.config.padded_vocab_length])
        return x

    def squad_projection(self, input_x):
        weight = self.normal_init_tensor(self.config.dtype,
                                         [self.config.hidden_size, 2],
                                         0, 0.02,
                                         "SquadW")
        bias = self.constant_init_tensor(self.config.dtype, (2,), 0, "SquadB")
        x = self.builder.aiOnnx.gemm([input_x, weight, bias])
        # x.shape: [batch_size * sequence_length, 2]
        start_logits = self.builder.aiOnnxOpset9.slice(
            [x], axes=[1], starts=[0], ends=[1])
        end_logits = self.builder.aiOnnxOpset9.slice(
            [x], axes=[1], starts=[1], ends=[2])

        start_logits = self.builder.reshape_const(
            self.builder.aiOnnx,
            [start_logits], [self.config.batch_size, self.config.sequence_length], debugContext="answer_start")
        end_logits = self.builder.reshape_const(
            self.builder.aiOnnx,
            [end_logits], [self.config.batch_size, self.config.sequence_length], debugContext="answer_end")

        return start_logits, end_logits

    def pooler(self, pooler_input):
        """
        Extract the cls tokens of all sequences that have been packed into a sample
        (these tokens have been rearranged to the back of the pack)
        """

        starts = self.config.sequence_length - self.config.max_sequences_per_pack
        ends = self.config.sequence_length
        pooler_input = self.builder.aiOnnxOpset9.slice([pooler_input], axes=[1],
                                                       starts=[starts], ends=[ends])

        pooler_input = self.builder.reshape_const(self.builder.aiOnnx, [pooler_input], [
                                                  self.config.batch_size, self.config.max_sequences_per_pack,
                                                  self.config.hidden_size])

        weight = self.normal_init_tensor(
            self.config.dtype,
            [self.config.hidden_size, self.config.hidden_size],
            0,
            0.02,
            "PoolW",
        )
        bias = self.constant_init_tensor(
            self.config.dtype, (self.config.hidden_size,), 0, "PoolB"
        )
        x = self.builder.aiOnnx.matmul([pooler_input, weight])
        x = self.builder.aiOnnx.add([x, bias])
        x = self.builder.aiOnnx.tanh([x])
        return x

    def nsp_head(self, input_x):
        x = self.builder.reshape_const(self.builder.aiOnnx, [input_x], [
                                       self.config.batch_size, self.config.sequence_length, self.config.hidden_size])

        x = self.pooler(x)

        cls_weight = self.normal_init_tensor(
            self.config.dtype, [self.config.hidden_size, 2], 0, 0.02, "NspW"
        )
        cls_bias = self.constant_init_tensor(
            self.config.dtype, (2,), 0, "NspB")
        x = self.builder.aiOnnx.matmul([x, cls_weight])
        x = self.builder.aiOnnx.add([x, cls_bias])
        return x

    def lm_prediction_head(self, input_x):
        dense_weight = self.normal_init_tensor(self.config.dtype,
                                               [self.config.hidden_size,
                                                self.config.hidden_size],
                                               0,
                                               0.02,
                                               "LMPredictionW")

        dense_bias = self.constant_init_tensor(
            self.config.dtype, (self.config.hidden_size,), 0, "LMPredictionB")

        x = self.builder.aiOnnx.gemm([input_x, dense_weight, dense_bias])

        x = self.intermediate_activation_function(x)
        x = self.norm(x)
        return x


def get_model(config, mode, block=None, initializers=None):
    # Specifying ai.onnx opset9 for the slice syntax
    builder = popart.Builder(opsets={
        "ai.onnx": 9,
        "ai.onnx.ml": 1,
        "ai.graphcore": 1
    })

    return Bert(config,
                builder=builder,
                initializers=initializers,
                execution_mode=mode)
