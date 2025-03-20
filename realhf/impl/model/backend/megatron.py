# Copyright 2025 Ant Group Inc.
# Copyright 2024 Wei Fu & Zhiyu Mei
# Licensed under the Apache License, Version 2.0 (the "License").

# Parts of code are modified from Megatron-LM.
# Copyright (c) 2024, NVIDIA CORPORATION. All rights reserved.
import collections
import dataclasses
import math
import pathlib
from contextlib import contextmanager
from typing import *

import torch
import torch.distributed as dist
import transformers
from torch._utils import _flatten_dense_tensors, _unflatten_dense_tensors

from realhf.api.core import model_api
from realhf.api.core.data_api import MicroBatchSpec, SequenceSample
from realhf.api.quickstart.model import (
    DistributedDataParallelConfig,
    MegatronConfig,
    OptimizerConfig,
)
from realhf.base import constants, logging
from realhf.base.datapack import flat2d
from realhf.base.monitor import CUDATimeMarkType, cuda_tmarked
from realhf.impl.model.backend.inference import PipelinableInferenceEngine
from realhf.impl.model.backend.pipe_runner import PipelineRunner, PipeTrainInstrSet
from realhf.impl.model.modules.mlp import get_activation_fn
from realhf.impl.model.nn.flatten_param import ContiguousParamSpec
from realhf.impl.model.nn.real_llm_api import ReaLModel
from realhf.impl.model.nn.real_llm_base import ReaLModelBlock
from realhf.impl.model.parallelism.pipeline_parallel.tensor_storage import TensorBuffer

try:
    # Monkey patch
    import megatron.core.optimizer as mcore_optim

    class DistributedOptimizer(mcore_optim.DistributedOptimizer):
        def get_model_parallel_group(self):
            return constants.parallelism_group()

    mcore_optim.DistributedOptimizer = DistributedOptimizer

    from megatron.core import parallel_state
    from megatron.core.distributed.distributed_data_parallel import (
        DistributedDataParallel,
    )
    from megatron.core.distributed.param_and_grad_buffer import ParamAndGradBuffer
    from megatron.core.optimizer import DistributedOptimizer, get_megatron_optimizer
    from megatron.core.optimizer.optimizer_config import (
        OptimizerConfig as MegatronOptimizerConfig,
    )
    from megatron.core.transformer.transformer_config import (
        TransformerConfig as MegatronTransformerConfig,
    )

    megatron_available = True
except (ModuleNotFoundError, ImportError):
    # importing megatron.core in CPU container will fail due to the requirement of apex
    # Here class types must be defined for type hinting
    class MegatronTransformerConfig:
        pass

    class DistributedDataParallel:
        pass

    class DistributedOptimizer:
        pass


if megatron_available:
    try:
        from megatron.core.optimizer_param_scheduler import OptimizerParamScheduler

        use_old_megatron = False
    except (ModuleNotFoundError, ImportError):
        # The above object is available in 0.9.0 but missing in 0.6.0
        from realhf.impl.model.backend.thirdparty.megatron.v0_6_0.lr_schduler import (
            OptimizerParamScheduler,
        )

        use_old_megatron = True


WITHIN_MEGATRON_CONTEXT = False

logger = logging.getLogger("Megatron Backend", "benchmark")


@contextmanager
def megatron_ctx():
    global WITHIN_MEGATRON_CONTEXT
    if WITHIN_MEGATRON_CONTEXT:
        raise RuntimeError("Megatron context is already set up. Destroy it first.")

    WITHIN_MEGATRON_CONTEXT = True

    grid = constants.grid()
    # TODO: implement context parallel.
    # TODO: implement expert parallel.

    # Build the data-parallel groups.
    g = constants.data_parallel_group()
    parallel_state._DATA_PARALLEL_GROUP = g
    parallel_state._DATA_PARALLEL_GROUP_GLOO = grid.get_data_parallel_group_gloo()
    parallel_state._DATA_PARALLEL_GLOBAL_RANKS = dist.get_process_group_ranks(g)
    parallel_state._DATA_PARALLEL_GROUP_WITH_CP = g
    parallel_state._DATA_PARALLEL_GROUP_WITH_CP_GLOO = (
        grid.get_data_parallel_group_gloo()
    )
    parallel_state._DATA_PARALLEL_GLOBAL_RANKS_WITH_CP = dist.get_process_group_ranks(g)

    # Build the context-parallel groups.
    parallel_state._CONTEXT_PARALLEL_GROUP = constants.self_group()
    parallel_state._CONTEXT_PARALLEL_GLOBAL_RANKS = [dist.get_rank()]

    # Build the model-parallel groups.
    parallel_state._MODEL_PARALLEL_GROUP = grid.get_model_parallel_group()

    # Build the tensor model-parallel groups.
    g = constants.model_parallel_group()
    parallel_state._TENSOR_MODEL_PARALLEL_GROUP = g

    # Build the pipeline model-parallel groups and embedding groups
    # (first and last rank in each pipeline model-parallel group).
    g = constants.pipe_parallel_group()
    parallel_state._PIPELINE_MODEL_PARALLEL_GROUP = g
    parallel_state._PIPELINE_GLOBAL_RANKS = dist.get_process_group_ranks(g)
    parallel_state._EMBEDDING_GROUP = grid.embedding_proc_group
    parallel_state._EMBEDDING_GLOBAL_RANKS = (
        dist.get_process_group_ranks(grid.embedding_proc_group)
        if grid.embedding_proc_group is not None
        else list(range(dist.get_world_size()))
    )
    parallel_state._POSITION_EMBEDDING_GROUP = grid.position_embedding_proc_group
    parallel_state._POSITION_EMBEDDING_GLOBAL_RANKS = (
        dist.get_process_group_ranks(grid.position_embedding_proc_group)
        if grid.position_embedding_proc_group is not None
        else list(range(dist.get_world_size()))
    )

    # Build the tensor + data parallel groups.
    parallel_state._TENSOR_AND_DATA_PARALLEL_GROUP = grid.tp_dp_proc_group
    parallel_state._TENSOR_AND_DATA_PARALLEL_GROUP_WITH_CP = grid.tp_dp_proc_group

    # Build the tensor + expert parallel groups
    parallel_state._EXPERT_MODEL_PARALLEL_GROUP = constants.self_group()
    parallel_state._TENSOR_AND_EXPERT_PARALLEL_GROUP = constants.model_parallel_group()
    g = constants.data_parallel_group()
    parallel_state._DATA_MODULO_EXPERT_PARALLEL_GROUP = g
    parallel_state._DATA_MODULO_EXPERT_PARALLEL_GROUP_WITH_CP = g
    parallel_state._DATA_MODULO_EXPERT_PARALLEL_GROUP_GLOO = (
        grid.get_data_parallel_group_gloo()
    )

    # Remove the global memory buffer for megatron to save GPU memory.
    parallel_state._GLOBAL_MEMORY_BUFFER = None
    yield
    WITHIN_MEGATRON_CONTEXT = False


def get_megatron_transformer_config(
    mconfig: model_api.ReaLModelConfig,
) -> MegatronTransformerConfig:
    nq = mconfig.hidden_dim // mconfig.head_dim
    n_group = nq // mconfig.n_kv_heads
    return MegatronTransformerConfig(
        num_layers=mconfig.n_layers,
        hidden_size=mconfig.hidden_dim,
        num_attention_heads=nq,
        num_query_groups=n_group,
        ffn_hidden_size=mconfig.intermediate_dim,
        kv_channels=mconfig.n_kv_heads,
        hidden_dropout=0.0,
        attention_dropout=mconfig.attn_pdrop,
        layernorm_epsilon=mconfig.layer_norm_epsilon,
        add_qkv_bias=mconfig.use_attention_bias,
        activation_func=get_activation_fn(mconfig.activation_function),
        rotary_interleaved=mconfig.rotary_interleaved,
        normalization=("RMSNorm" if mconfig.layer_norm_type == "rms" else "LayerNorm"),
        attention_softmax_in_fp32=True,
        apply_query_key_layer_scaling=mconfig.scale_attn_by_inverse_layer_idx,
    )


@dataclasses.dataclass
class MegatronEngine:
    ddp: DistributedDataParallel
    optim: DistributedOptimizer
    lr_scheduler: Any

    def zero_grad(self, set_to_none=True):
        self.ddp.zero_grad_buffer()
        self.optim.zero_grad(set_to_none=set_to_none)

    def _all_reduce_layernorm_grads(self):
        if not (
            constants.sequence_parallel() and constants.model_parallel_world_size() > 1
        ):
            return
        real_model: ReaLModel = self.ddp.module
        grads = []
        for i in range(real_model.layer_idx_start, real_model.layer_idx_end):
            if i == 0:
                continue
            elif i == real_model.config.n_layers + 1:
                continue
            else:
                assert 0 < i < real_model.config.n_layers + 1
                layer: ReaLModelBlock = real_model.layers[
                    i - real_model.layer_idx_start
                ]
                grads.append(layer.attn.c_attn.ln.weight.main_grad)
                if getattr(layer.attn.c_attn.ln, "bias", None) is not None:
                    grads.append(layer.attn.c_attn.ln.bias.main_grad)
                grads.append(layer.mlp.ln.weight.main_grad)
                if getattr(layer.mlp.ln, "bias", None) is not None:
                    grads.append(layer.mlp.ln.bias.main_grad)
                if i == real_model.config.n_layers:
                    grads.append(layer.ln_f.weight.main_grad)
                    if getattr(layer.ln_f, "bias", None) is not None:
                        grads.append(layer.ln_f.bias.main_grad)

        assert all(x is not None for x in grads)
        coalesced = _flatten_dense_tensors(grads)
        dist.all_reduce(coalesced, group=constants.model_parallel_group())
        for buf, synced in zip(grads, _unflatten_dense_tensors(coalesced, grads)):
            buf.copy_(synced)

    def _all_reduce_word_embedding_grads(self):
        real_model: ReaLModel = self.ddp.module
        if not real_model.config.tied_embedding or real_model.config.is_critic:
            return
        pp_size = constants.pipe_parallel_world_size()
        pp_rank = constants.pipe_parallel_rank()
        if pp_size == 1:
            return
        if pp_rank not in [0, pp_size - 1]:
            return

        if pp_rank == 0:
            grad = real_model.layers[0].wte.weight.main_grad
        else:
            grad = real_model.layers[-1].weight.main_grad

        dist.all_reduce(grad, group=constants.grid().embedding_proc_group)

    def finalize_grads(self):
        self.ddp.finish_grad_sync()
        self._all_reduce_layernorm_grads()
        self._all_reduce_word_embedding_grads()


@dataclasses.dataclass
class PipeTrainInstrSetForMegatron(PipeTrainInstrSet):
    # NOTE: merge DistributedDataParallel and DistributedOptimizer into one class
    # to remain consistent with DeepSpeed's API
    engine: MegatronEngine
    num_micro_batches: int

    def __post_init__(self):
        self._no_sync_context = None
        self.disable_grad_sync()

    def disable_grad_sync(self):
        if self._no_sync_context is None:
            self._no_sync_context = self.engine.ddp.no_sync()
            self._no_sync_context.__enter__()

    def enable_grad_sync(self):
        if self._no_sync_context is not None:
            self._no_sync_context.__exit__(None, None, None)
            self._no_sync_context = None

    @cuda_tmarked("bwd", CUDATimeMarkType.backward)
    def _exec_backward_pass(
        self,
        module: ReaLModel,
        tensor_buffer: TensorBuffer,
        stage_id: int,
        micro_batch_id: int,
        step_id: int,
    ):
        output_x = tensor_buffer.get("batch_output_x", micro_batch_id, remove=True)

        if micro_batch_id == self.num_micro_batches - 1:
            self.enable_grad_sync()

        is_last_stage = constants.is_last_pipe_stage()
        if is_last_stage:
            loss: torch.Tensor = tensor_buffer.get(
                "losses", micro_batch_id, remove=True
            )
            loss.backward()
            tensor_buffer.put("losses", micro_batch_id, loss.detach().clone())
            return

        grad = tensor_buffer.get("grad", micro_batch_id, remove=True)
        output_tensor = output_x.pp_output
        torch.autograd.backward(tensors=output_tensor, grad_tensors=grad)

    def _exec_reduce_grads(
        self,
        module: ReaLModel,
        tensor_buffer: TensorBuffer,
        stage_id: int,
        micro_batch_id: int,
        step_id: int,
    ):
        # self.engine.ddp.start_grad_sync()
        self.engine.finalize_grads()

    @cuda_tmarked("opt", CUDATimeMarkType.optim_step)
    def _exec_optimizer_step(
        self,
        module: ReaLModel,
        tensor_buffer: TensorBuffer,
        stage_id: int,
        micro_batch_id: int,
        step_id: int,
    ):
        update_successful, grad_norm, num_zeros_in_grad = self.engine.optim.step()

        version_steps = tensor_buffer.get("version_steps", 0)
        if update_successful:
            incr = version_steps - self.engine.lr_scheduler.num_steps
            self.engine.lr_scheduler.step(incr)
        if constants.data_parallel_rank() == 0 and constants.model_parallel_rank() == 0:
            logger.info(
                f"Model name {constants.model_name()}, "
                f"Pipeline rank {constants.pipe_parallel_rank()}. "
                f"Update success? {update_successful}. "
                f"Grad Norm: {grad_norm}. "
                f"Current loss scale: {self.engine.optim.get_loss_scale()}. "
                f"Learning rate: {[param_group['lr'] for param_group in self.engine.optim.param_groups]}. "
            )
        return update_successful, grad_norm, num_zeros_in_grad


class ReaLMegatronEngine(model_api.PipelinableEngine):

    def __init__(self, module: ReaLModel, megatron_engine: MegatronEngine):
        self.module = module

        self.inf_engine = PipelinableInferenceEngine(module)
        if constants.pipe_parallel_world_size() > 1:
            self.pipe_runner = self.inf_engine.pipe_runner

        # NOTE: In profiler, module could be not a instance of ReaLModel.
        self.device = module.device if hasattr(module, "device") else None
        self.dtype = module.dtype if hasattr(module, "dtype") else None

        self.engine = megatron_engine

    def train(self, mode: bool = True):
        self.module.train(mode)
        self.engine.ddp.train(mode)
        return self

    def eval(self):
        self.module.eval()
        self.engine.ddp.eval()
        return self

    def train_batch(
        self,
        input_: SequenceSample,
        mb_spec: MicroBatchSpec,
        loss_fn: Callable,
        loss_weight_fn: Callable,
        token_normalize_scope: str,
        version_steps: int,
    ):
        with megatron_ctx():
            self.engine.zero_grad()
            if constants.pipe_parallel_world_size() > 1:
                mb_inputs = input_.synced_data_parallel_split(
                    MicroBatchSpec.new(
                        mb_spec,
                        n_mbs=mb_spec.n_mbs * self.pipe_runner.default_train_mbs,
                    )
                )
                # Fusing the minibatched forward-backward in a pipeline training schedule.
                instr_set = PipeTrainInstrSetForMegatron(self.engine, len(mb_inputs))
                # NOTE: When training with pipeline parallel, num micro batches should be
                # larger than 2 x num_pipeline_stages to avoid idle time.
                return self.pipe_runner.train_batch(
                    instr_set=instr_set,
                    input_=input_,
                    mb_spec=mb_spec,
                    loss_fn=loss_fn,
                    loss_weight_fn=loss_weight_fn,
                    token_normalize_scope=token_normalize_scope,
                    version_steps=version_steps,
                )

            mb_inputs = input_.synced_data_parallel_split(mb_spec)
            total_loss_weight = torch.tensor(
                sum([loss_weight_fn(mb) for mb in mb_inputs]), dtype=torch.float32
            )
            if token_normalize_scope == "global":
                dist.all_reduce(
                    total_loss_weight, group=constants.data_parallel_group()
                )
            if total_loss_weight == 0:
                raise model_api.ZeroTotalLossWeightException(
                    "The sum of loss weights of all micro batches is zero."
                )

            if constants.parallelism_rank() == 0:
                logger.info(
                    f"MB spec: {mb_spec}, #mbs={len(mb_inputs)}, "
                    f"#tokens: {input_.data['packed_input_ids'].shape[0]}, "
                    f"pp_size={constants.pipe_parallel_world_size()}, "
                    f"#tokens per mbs: {[mb.data['packed_input_ids'].shape[0] for mb in mb_inputs]}"
                )
            no_sync_ctx = self.engine.ddp.no_sync()
            no_sync_ctx.__enter__()
            stat = collections.defaultdict(int)
            for i, mb_input in enumerate(mb_inputs):
                if i == len(mb_inputs) - 1:
                    no_sync_ctx.__exit__(None, None, None)
                input_lens = torch.tensor(
                    flat2d(mb_input.seqlens["packed_input_ids"]),
                    dtype=torch.int32,
                    device="cuda",
                )
                max_seqlen = int(max(input_lens))
                cu_seqlens = torch.nn.functional.pad(input_lens.cumsum(0), (1, 0)).int()
                model_output = self.engine.ddp(
                    packed_input_ids=mb_input.data["packed_input_ids"],
                    cu_seqlens=cu_seqlens,
                    max_seqlen=max_seqlen,
                ).logits
                loss, _stat = loss_fn(model_output, mb_input)
                loss_scale = loss_weight_fn(mb_inputs[i]) / total_loss_weight
                if token_normalize_scope == "global":
                    # Megatron will average gradients across DP ranks.
                    # If we normalize loss across micro batches of all DP ranks,
                    # we should revert the effect of gradient averaging in megatron
                    # to make sure loss from each token is scaled properly.
                    loss_scale *= constants.data_parallel_world_size()
                loss_scale *= self.engine.optim.get_loss_scale().item()
                loss *= loss_scale
                with cuda_tmarked("bwd", CUDATimeMarkType.backward):
                    loss.backward()
                for k, v in _stat.items():
                    stat[k] += v

            self.engine.finalize_grads()
            self._step(version_steps)

            return stat

    @torch.no_grad()
    def forward(
        self,
        input_: SequenceSample,
        mb_spec: MicroBatchSpec,
        output_seqlens: List[List[int]] | None = None,
        post_hook: Callable[[torch.Tensor, SequenceSample], Any] | None = None,
        aggregate_fn: Callable[[List[Any]], Any] = torch.cat,
    ):
        return self.inf_engine.forward(
            input_=input_,
            mb_spec=mb_spec,
            output_seqlens=output_seqlens,
            post_hook=post_hook,
            aggregate_fn=aggregate_fn,
        )

    @torch.no_grad()
    def generate(
        self,
        input_: SequenceSample,
        mb_spec: MicroBatchSpec,
        tokenizer: transformers.PreTrainedTokenizerFast,
        gconfig: model_api.GenerationHyperparameters = dataclasses.field(
            default_factory=model_api.GenerationHyperparameters
        ),
    ):
        return self.inf_engine.generate(
            input_=input_,
            tokenizer=tokenizer,
            gconfig=gconfig,
            mb_spec=mb_spec,
        )

    # wrapper for profiler
    @cuda_tmarked("opt", CUDATimeMarkType.optim_step)
    def _step(self, version_steps):
        update_successful, grad_norm, _ = self.engine.optim.step()
        if update_successful:
            incr = version_steps - self.engine.lr_scheduler.num_steps
            self.engine.lr_scheduler.step(incr)
        if constants.data_parallel_rank() == 0 and constants.model_parallel_rank() == 0:
            logger.info(
                f"Megatron backend update success? {update_successful}. "
                f"Grad Norm: {grad_norm}. "
                f"Current loss scale: {self.engine.optim.get_loss_scale()}. "
                f"Learning rate: {[param_group['lr'] for param_group in self.engine.optim.param_groups]}. "
            )


@dataclasses.dataclass
class MegatronTrainBackend(model_api.ModelBackend, MegatronConfig):
    bf16: bool = False
    optimizer: OptimizerConfig = dataclasses.field(default_factory=OptimizerConfig)

    def _initialize(
        self, model: model_api.Model, spec: model_api.FinetuneSpec
    ) -> model_api.Model:
        module = model.module
        if not isinstance(module, ReaLModel):
            raise ValueError("MegatronTrainBackend only supports ReaLModel.")
        if isinstance(self.ddp, dict):
            self.ddp = DistributedDataParallelConfig(**self.ddp)
        with megatron_ctx():
            if use_old_megatron:
                module = DistributedDataParallel(
                    config=get_megatron_transformer_config(module.config),
                    module=module,
                    data_parallel_group=constants.data_parallel_group(),
                    accumulate_allreduce_grads_in_fp32=self.ddp.grad_reduce_in_fp32,
                    overlap_grad_reduce=self.ddp.overlap_grad_reduce,
                    use_distributed_optimizer=self.ddp.use_distributed_optimizer,
                    expert_data_parallel_group=None,
                    disable_bucketing=False,
                    check_for_nan_in_grad=self.ddp.check_for_nan_in_grad,
                    bucket_size=self.ddp.bucket_size,
                )
            else:
                module = DistributedDataParallel(
                    config=get_megatron_transformer_config(module.config),
                    ddp_config=self.ddp,
                    module=module,
                    disable_bucketing=False,
                )

        real_model: ReaLModel = module.module
        if self.ddp.use_distributed_optimizer:
            # Remap parameters.
            assert len(module.buffers) == 1
            param_grad_buf: ParamAndGradBuffer = module.buffers[0]

            # Map Megatron flattened parameters to ReaLModel!
            real_model.contiguous_param = param_grad_buf.param_data

            # Sanity checks.
            assert real_model._param_size == param_grad_buf.numel, (
                real_model._param_size,
                param_grad_buf.numel,
                module.bucket_size,
            )
            for n, p in real_model.layers.named_parameters():
                n = ".".join(
                    [
                        str(real_model.layer_idx_start + int(n.split(".")[0])),
                        n.split(".", 1)[1],
                    ]
                )
                idx_start, idx_end, _ = param_grad_buf.param_index_map[p]
                assert real_model._param_spec[n].start_idx == idx_start
                assert real_model._param_spec[n].end_idx == idx_end
                assert real_model._param_spec[n].shape == p.shape
                assert torch.allclose(
                    p,
                    real_model.contiguous_param[idx_start:idx_end].view(p.shape),
                )

        betas = (self.optimizer.beta1, self.optimizer.beta2)
        wd = self.optimizer.weight_decay
        lr = self.optimizer.lr
        opt_cfg = MegatronOptimizerConfig(
            optimizer=self.optimizer.type,
            bf16=self.bf16,
            fp16=not self.bf16,
            lr=lr,
            min_lr=self.optimizer.min_lr_ratio * lr,
            weight_decay=wd,
            params_dtype=real_model.dtype,
            initial_loss_scale=self.optimizer.initial_loss_scale,
            min_loss_scale=self.optimizer.min_loss_scale,
            loss_scale_window=self.optimizer.loss_scale_window,
            hysteresis=self.optimizer.hysteresis,
            adam_beta1=betas[0],
            adam_beta2=betas[1],
            adam_eps=self.optimizer.eps,
            use_distributed_optimizer=self.ddp.use_distributed_optimizer,
            overlap_grad_reduce=self.ddp.overlap_grad_reduce,
            overlap_param_gather=self.ddp.overlap_param_gather,
            clip_grad=self.optimizer.gradient_clipping,
            log_num_zeros_in_grad=False,
        )

        with megatron_ctx():
            # no_weight_decay_cond and scale_lr_cond have the following signature:
            # foo(name: str, param: torch.Tensor) -> bool
            optimizer = get_megatron_optimizer(
                opt_cfg,
                [module],
                no_weight_decay_cond=lambda n, p: any(
                    k in n for k in ["bias", "ln.weight", "ln_f.weight"]
                ),
                scale_lr_cond=None,
                lr_mult=1.0,
            )

            warmup_steps_proportion = self.optimizer.warmup_steps_proportion
            warmup_steps = int(warmup_steps_proportion * spec.total_train_steps)
            lr_scheduler = OptimizerParamScheduler(
                optimizer,
                init_lr=0.0 if warmup_steps_proportion > 0 else lr,
                max_lr=lr,
                min_lr=self.optimizer.min_lr_ratio * lr,
                lr_warmup_steps=warmup_steps,
                lr_decay_steps=spec.total_train_steps - warmup_steps,
                lr_decay_style=self.optimizer.lr_scheduler_type,
                start_wd=wd,
                end_wd=wd,
                wd_incr_steps=spec.total_train_steps,
                wd_incr_style="constant",
            )

        mg_engine = MegatronEngine(module, optimizer, lr_scheduler)
        model.module = ReaLMegatronEngine(real_model, mg_engine)
        model.backend_name = "megatron"
        return model

    def destroy(self, model: model_api.Model):
        assert isinstance(model.module, ReaLMegatronEngine)
        optimizer = model.module.engine.optim
        # The Megatron backend will register forward hooks that
        # create circular references (grad -> param -> grad).
        # Deleting models directly will not release the memory.
        # We must disable hooks at first.
        if self.ddp.use_distributed_optimizer and self.ddp.overlap_param_gather:
            optimizer.disable_pre_hook()

    def save(self, model: model_api.Model, save_dir: str):
        assert isinstance(model.module, ReaLMegatronEngine)
        optimizer = model.module.engine.optim
        param_state = optimizer.get_parameter_state_fs_bucket_space()
        sd = optimizer.state_dict()
        dp = constants.data_parallel_rank()
        pp = constants.pipe_parallel_rank()
        tp = constants.model_parallel_rank()
        # HACK: (bowei) I'm not sure whether there's duplicated information.
        torch.save(
            sd, pathlib.Path(save_dir) / f"megatron_optim_sd_d{dp}p{pp}t{tp}.mckpt"
        )
        torch.save(
            param_state,
            pathlib.Path(save_dir) / f"megatron_optim_param_sd_d{dp}p{pp}t{tp}.mckpt",
        )

    def load(self, model: model_api.Model, load_dir: str):
        assert isinstance(model.module, ReaLMegatronEngine)
        optimizer = model.module.engine.optim

        dp = constants.data_parallel_rank()
        pp = constants.pipe_parallel_rank()
        tp = constants.model_parallel_rank()

        sd = torch.load(
            pathlib.Path(load_dir) / f"megatron_optim_sd_d{dp}p{pp}t{tp}.mckpt"
        )
        optimizer.load_state_dict(sd)

        param_state = torch.load(
            pathlib.Path(load_dir) / f"megatron_optim_param_sd_d{dp}p{pp}t{tp}.mckpt"
        )
        optimizer.load_parameter_state_from_fs_bucket_space(param_state)


model_api.register_backend("megatron", MegatronTrainBackend)
