from transformers.trainer import (
    Trainer,
    TrainerState,
    TrainOutput,
    has_length,
    is_sagemaker_mp_enabled,
    get_model_param_count,
    speed_metrics,
    deepspeed_init,
    TRAINER_STATE_NAME,
)
from transformers.trainer_callback import ExportableState
import loraprune.utils as utils
import loraprune.utils_ul as utils_ul
import math
import sys
import time
import torch
from torch import nn
from torch.utils.data import DataLoader, DistributedSampler, RandomSampler
from transformers.trainer_pt_utils import IterableDatasetShard
from transformers.utils import logging, is_torch_xla_available, is_apex_available
from transformers.debug_utils import DebugOption, DebugUnderflowOverflow
import os
from packaging import version
import shutil

if is_apex_available():
    from apex import amp

parsed_torch_version_base = version.parse(version.parse(torch.__version__).base_version)

is_torch_less_than_1_11 = parsed_torch_version_base < version.parse("1.11")
logger = logging.get_logger(__name__)

class LoRAPruneTrainer(Trainer):
    def __init__(
        self,
        model,
        forget_dataset=None,
        retain_dataset=None,
        args=None,
        data_collator=None,
        ratio=0.5,
        init_ratio=0,
        warmup_iters=0.1,
        cooldown_iters=0.1,
        prune_freq=10,
        prune_metric='lora',
        unlearning_threshold=0.1,
        min_retain_performance=0.9,
    ):
        # Initialize parent with retain dataset as main training data
        super().__init__(
            model=model,
            train_dataset=retain_dataset, # will be used in _inner_training_loop (get_train_dataloader)
            # eval_dataset=retain_eval_dataset,
            args=args,
            data_collator=data_collator,
        )
        
        # Store datasets
        self.forget_dataset = forget_dataset
        
        # Store pruning parameters
        self.ratio = ratio
        self.init_ratio = init_ratio
        self.warmup_iters = warmup_iters
        self.cooldown_iters = cooldown_iters
        self.prune_freq = prune_freq
        self.prune_metric = prune_metric
        
        # Store unlearning parameters
        self.unlearning_threshold = unlearning_threshold
        self.min_retain_performance = min_retain_performance
        
        # Initialize tracking metrics
        self.forget_loss_history = []
        self.retain_loss_history = []
        self.pruning_history = []

    def _inner_training_loop(
        self, batch_size=None, args=None, resume_from_checkpoint=None, trial=None, ignore_keys_for_eval=None
    ):
        self._train_batch_size = batch_size
        # Data loader and number of training steps
        train_dataloader = self.get_train_dataloader()
        forget_dataloader = None if self.forget_dataset is None else DataLoader(
            self.forget_dataset,
            batch_size=self.args.per_device_train_batch_size,
            shuffle=True,
            collate_fn=self.data_collator,
        )

        # Setting up training control variables
        total_train_batch_size = args.train_batch_size * args.gradient_accumulation_steps * args.world_size

        len_dataloader = None
        if has_length(train_dataloader) and (forget_dataloader is None or has_length(forget_dataloader)):
            # Calculate total length combining both dataloaders
            forget_len = len(forget_dataloader) if forget_dataloader is not None else 0
            retain_len = len(train_dataloader)
            len_dataloader = forget_len + retain_len  # total number of batches per epoch
            
            num_update_steps_per_epoch = len_dataloader // args.gradient_accumulation_steps
            num_update_steps_per_epoch = max(num_update_steps_per_epoch, 1)
            
            # Calculate total examples from both sets
            forget_examples = self.num_examples(forget_dataloader) if forget_dataloader is not None else 0
            retain_examples = self.num_examples(train_dataloader)
            num_examples = forget_examples + retain_examples
            
            if args.max_steps > 0:
                max_steps = args.max_steps
                num_train_epochs = args.max_steps // num_update_steps_per_epoch + int(
                    args.max_steps % num_update_steps_per_epoch > 0
                )
                num_train_samples = args.max_steps * total_train_batch_size
            else:
                max_steps = math.ceil(args.num_train_epochs * num_update_steps_per_epoch)
                num_train_epochs = math.ceil(args.num_train_epochs)
                num_train_samples = num_examples * args.num_train_epochs
        elif args.max_steps > 0:
            max_steps = args.max_steps
            num_train_epochs = sys.maxsize
            num_update_steps_per_epoch = max_steps
            num_examples = total_train_batch_size * args.max_steps
            num_train_samples = args.max_steps * total_train_batch_size
        else:
            raise ValueError(
                "args.max_steps must be set to a positive value if dataloader does not have a length"
            )

        delay_optimizer_creation = False

        self.state = TrainerState(
            stateful_callbacks=[
                cb for cb in self.callback_handler.callbacks + [self.control] if isinstance(cb, ExportableState)
            ]
        )
        self.state.is_hyper_param_search = trial is not None

        # Activate gradient checkpointing if needed
        if args.gradient_checkpointing:
            self.model.gradient_checkpointing_enable()

        model = self._wrap_model(self.model_wrapped)

        if is_sagemaker_mp_enabled() and resume_from_checkpoint is not None:
            self._load_from_checkpoint(resume_from_checkpoint, model)

        # for the rest of this function `model` is the outside model, whether it was wrapped or not
        if model is not self.model:
            self.model_wrapped = model

        self.create_optimizer_and_scheduler(num_training_steps=max_steps)

        # Check if saved optimizer or scheduler states exist
        self._load_optimizer_and_scheduler(resume_from_checkpoint)

        # important: at this point:
        # self.model         is the Transformers Model
        # self.model_wrapped is DDP(Transformers Model), Deepspeed(Transformers Model), etc.
        total_params = kept_params = sum([p.numel() if not p.requires_grad else 0 for p in model.parameters()])
        logger.info("***** Running training *****")
        logger.info(f"  Num examples = {num_examples:,}")
        logger.info(f"  Num Epochs = {num_train_epochs:,}")
        logger.info(f"  Instantaneous batch size per device = {args.per_device_train_batch_size:,}")
        logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_train_batch_size:,}")
        logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
        logger.info(f"  Total optimization steps = {max_steps:,}")
        logger.info(f"  Number of trainable parameters = {get_model_param_count(model, trainable_only=True):,}")

        self.state.epoch = 0
        start_time = time.time()
        
        # Initialize training variables
        epochs_trained = 0
        steps_trained_in_current_epoch = 0
        steps_trained_progress_bar = None
        
        self.control = self.callback_handler.on_train_begin(args, self.state, self.control)

        # Check if continuing training from a checkpoint
        if resume_from_checkpoint is not None and os.path.isfile(
            os.path.join(resume_from_checkpoint, TRAINER_STATE_NAME)
        ):
            self.state = TrainerState.load_from_json(os.path.join(resume_from_checkpoint, TRAINER_STATE_NAME))
            epochs_trained = self.state.global_step // num_update_steps_per_epoch
            if not args.ignore_data_skip:
                steps_trained_in_current_epoch = self.state.global_step % (num_update_steps_per_epoch)
                steps_trained_in_current_epoch *= args.gradient_accumulation_steps
            else:
                steps_trained_in_current_epoch = 0

            logger.info("  Continuing training from checkpoint, will skip to saved global_step")
            logger.info(f"  Continuing training from epoch {epochs_trained}")
            logger.info(f"  Continuing training from global step {self.state.global_step}")


        # Update the references
        self.callback_handler.model = self.model
        self.callback_handler.optimizer = self.optimizer
        self.callback_handler.lr_scheduler = self.lr_scheduler
        self.callback_handler.train_dataloader = train_dataloader
        if self.hp_name is not None and self._trial is not None:
            # use self._trial because the SigOpt/Optuna hpo only call `_hp_search_setup(trial)` instead of passing trial
            # parameter to Train when using DDP.
            self.state.trial_name = self.hp_name(self._trial)

        self.state.trial_params = None
        # This should be the same if the state has been saved but in case the training arguments changed, it's safer
        # to set this after the load.
        self.state.max_steps = max_steps
        self.state.num_train_epochs = num_train_epochs
        self.state.is_local_process_zero = self.is_local_process_zero()
        self.state.is_world_process_zero = self.is_world_process_zero()

        # tr_loss is a tensor to avoid synchronization of TPUs through .item()
        tr_loss = torch.tensor(0.0).to(args.device)
        # _total_loss_scalar is updated everytime .item() has to be called on tr_loss and stores the sum of all losses
        self._total_loss_scalar = 0.0
        self._globalstep_last_logged = self.state.global_step
        model.zero_grad()

        total_batched_samples = 0
        if self.prune_metric == 'grad':
            utils.unfreeze(model)

        # Initialize separate sensitivity dictionaries for forget and retain sets
        forget_sensitivity_dict = utils.init_sensitivity_dict(model)
        retain_sensitivity_dict = utils.init_sensitivity_dict(model)
        
        for epoch in range(epochs_trained, num_train_epochs):
            if isinstance(train_dataloader, DataLoader) and isinstance(train_dataloader.sampler, DistributedSampler):
                train_dataloader.sampler.set_epoch(epoch)
            elif hasattr(train_dataloader, "dataset") and isinstance(train_dataloader.dataset, IterableDatasetShard):
                train_dataloader.dataset.set_epoch(epoch)
            
            # Set epoch for forget_dataloader if it exists
            if forget_dataloader is not None:
                if isinstance(forget_dataloader, DataLoader) and isinstance(forget_dataloader.sampler, DistributedSampler):
                    forget_dataloader.sampler.set_epoch(epoch)
                elif hasattr(forget_dataloader, "dataset") and isinstance(forget_dataloader.dataset, IterableDatasetShard):
                    forget_dataloader.dataset.set_epoch(epoch)

            # Reset the past mems state at the beginning of each epoch if necessary.
            if args.past_index >= 0:
                self._past = None

            steps_in_epoch = (
                len(train_dataloader)  # Keep using retain dataloader length as base
                if len_dataloader is not None
                else args.max_steps * args.gradient_accumulation_steps
            )
            self.control = self.callback_handler.on_epoch_begin(args, self.state, self.control)

            if epoch == epochs_trained and resume_from_checkpoint is not None and steps_trained_in_current_epoch == 0:
                self._load_rng_state(resume_from_checkpoint)

            rng_to_sync = False
            steps_skipped = 0

            # First process forget set if it exists
            if forget_dataloader is not None:
                for step, inputs in enumerate(forget_dataloader):
                    total_batched_samples += 1
                    if step % args.gradient_accumulation_steps == 0:
                        self.control = self.callback_handler.on_step_begin(args, self.state, self.control)

                    tr_loss_step = self.training_step(model, inputs)  # Add is_forget flag to differentiate
                    
                    if (
                        args.logging_nan_inf_filter
                        and not is_torch_xla_available()
                        and (torch.isnan(tr_loss_step) or torch.isinf(tr_loss_step))
                    ):
                        # if loss is nan or inf simply add the average of previous logged losses
                        tr_loss += tr_loss / (1 + self.state.global_step - self._globalstep_last_logged)
                    else:
                        tr_loss += tr_loss_step

                    self.current_flos += float(self.floating_point_ops(inputs))

                    # Optimizer step for deepspeed must be called on every step regardless of the value of gradient_accumulation_steps
                    if self.deepspeed:
                        self.deepspeed.step()

                    if total_batched_samples % args.gradient_accumulation_steps == 0 or (
                        # last step in epoch but step is always smaller than gradient_accumulation_steps
                        steps_in_epoch <= args.gradient_accumulation_steps
                        and (step + 1) == steps_in_epoch
                    ):
                        # Gradient clipping
                        if args.max_grad_norm is not None and args.max_grad_norm > 0 and not self.deepspeed:
                            # deepspeed does its own clipping

                            # AMP: gradients need unscaling
                            # self.scaler.unscale_(self.optimizer)

                            if is_sagemaker_mp_enabled() and args.fp16:
                                grad_norm = self.optimizer.clip_master_grads(args.max_grad_norm)
                            elif hasattr(self.optimizer, "clip_grad_norm"):
                                # Some optimizers (like the sharded optimizer) have a specific way to do gradient clipping
                                grad_norm = self.optimizer.clip_grad_norm(args.max_grad_norm)
                            elif hasattr(model, "clip_grad_norm_"):
                                # Some models (like FullyShardedDDP) have a specific way to do gradient clipping
                                grad_norm = model.clip_grad_norm_(args.max_grad_norm)
                            else:
                                # Revert to normal clipping otherwise, handling Apex or full precision
                                grad_norm = nn.utils.clip_grad_norm_(
                                    amp.master_params(self.optimizer) if self.use_apex else model.parameters(),
                                    args.max_grad_norm,
                                )

                        # Optimizer step
                        if not self.deepspeed:
                            forget_sensitivity_dict = utils.update_sensitivity_dict(model, forget_sensitivity_dict, self.prune_metric)
                        ratio = utils.schedule_sparsity_ratio(self.state.global_step, self.state.max_steps,
                                                              self.warmup_iters,
                                                              self.cooldown_iters, self.init_ratio, self.ratio)

                        # ratio = 0.05
                        # if (self.state.global_step) % self.prune_freq == 0 and ratio > self.init_ratio and ratio < self.ratio:
                        #     utils.local_prune(model, sensitivity_dict, ratio, self.ratio)

                        optimizer_was_run = True
                        if self.deepspeed:
                            pass  # called outside the loop
                        # self.optimizer.step() # Do not call optimizer step for forget set

                        # if optimizer_was_run and not self.deepspeed:
                        #     self.lr_scheduler.step()  # Do not call lr scheduler for forget set

                        model.zero_grad()
                        self.state.global_step += 1
                        self.state.epoch = epoch + (step + 1 + steps_skipped) / steps_in_epoch
                        self.control = self.callback_handler.on_step_end(args, self.state, self.control)

                        self._maybe_log_save_evaluate(tr_loss, grad_norm if grad_norm is not None else None, model, trial, epoch, ignore_keys_for_eval)
                    else:
                        self.control = self.callback_handler.on_substep_end(args, self.state, self.control)

                    if self.control.should_epoch_stop or self.control.should_training_stop:
                        break

            # Then process retain set
            for step, inputs in enumerate(train_dataloader):
                total_batched_samples += 1
                if rng_to_sync:
                    self._load_rng_state(resume_from_checkpoint)
                    rng_to_sync = False

                if step % args.gradient_accumulation_steps == 0:
                    self.control = self.callback_handler.on_step_begin(args, self.state, self.control)

                tr_loss_step = self.training_step(model, inputs)
                
                if (
                    args.logging_nan_inf_filter
                    and not is_torch_xla_available()
                    and (torch.isnan(tr_loss_step) or torch.isinf(tr_loss_step))
                ):
                    # if loss is nan or inf simply add the average of previous logged losses
                    tr_loss += tr_loss / (1 + self.state.global_step - self._globalstep_last_logged)
                else:
                    tr_loss += tr_loss_step

                self.current_flos += float(self.floating_point_ops(inputs))

                # Optimizer step for deepspeed must be called on every step regardless of the value of gradient_accumulation_steps
                if self.deepspeed:
                    self.deepspeed.step()

                if total_batched_samples % args.gradient_accumulation_steps == 0 or (
                    # last step in epoch but step is always smaller than gradient_accumulation_steps
                    steps_in_epoch <= args.gradient_accumulation_steps
                    and (step + 1) == steps_in_epoch
                ):
                    # Gradient clipping
                    if args.max_grad_norm is not None and args.max_grad_norm > 0 and not self.deepspeed:
                        # deepspeed does its own clipping

                        # AMP: gradients need unscaling
                        # self.scaler.unscale_(self.optimizer)

                        if is_sagemaker_mp_enabled() and args.fp16:
                            grad_norm = self.optimizer.clip_master_grads(args.max_grad_norm)
                        elif hasattr(self.optimizer, "clip_grad_norm"):
                            # Some optimizers (like the sharded optimizer) have a specific way to do gradient clipping
                            grad_norm = self.optimizer.clip_grad_norm(args.max_grad_norm)
                        elif hasattr(model, "clip_grad_norm_"):
                            # Some models (like FullyShardedDDP) have a specific way to do gradient clipping
                            grad_norm = model.clip_grad_norm_(args.max_grad_norm)
                        else:
                            # Revert to normal clipping otherwise, handling Apex or full precision
                            grad_norm = nn.utils.clip_grad_norm_(
                                amp.master_params(self.optimizer) if self.use_apex else model.parameters(),
                                args.max_grad_norm,
                            )

                    # Optimizer step
                    if not self.deepspeed:
                        retain_sensitivity_dict = utils.update_sensitivity_dict(model, retain_sensitivity_dict, self.prune_metric)
                    ratio = utils.schedule_sparsity_ratio(self.state.global_step, self.state.max_steps,
                                                          self.warmup_iters,
                                                          self.cooldown_iters, self.init_ratio, self.ratio)

                    if (self.state.global_step) % self.prune_freq == 0 and ratio > self.init_ratio and ratio < self.ratio:
                        # Compute dual sensitivity scores
                        dual_sensitivity = {}
                        for name in forget_sensitivity_dict.keys():
                            dual_sensitivity[name] = utils_ul.compute_dual_sensitivity(
                                forget_sensitivity_dict[name],
                                retain_sensitivity_dict[name]
                            )
                        
                        # Apply unlearning-aware pruning
                        utils_ul.unlearning_prune(
                            model, 
                            dual_sensitivity, 
                            ratio,
                            self.unlearning_threshold
                        )

                    optimizer_was_run = True
                    if self.deepspeed:
                        pass  # called outside the loop
                    self.optimizer.step()

                    if optimizer_was_run and not self.deepspeed:
                        self.lr_scheduler.step()

                    model.zero_grad()
                    self.state.global_step += 1
                    self.state.epoch = epoch + (step + 1 + steps_skipped) / steps_in_epoch
                    self.control = self.callback_handler.on_step_end(args, self.state, self.control)

                    self._maybe_log_save_evaluate(tr_loss, grad_norm if grad_norm is not None else None, model, trial, epoch, ignore_keys_for_eval)
                else:
                    self.control = self.callback_handler.on_substep_end(args, self.state, self.control)

                if self.control.should_epoch_stop or self.control.should_training_stop:
                    break
            if step < 0:
                logger.warning(
                    "There seems to be not a single sample in your epoch_iterator, stopping training at step"
                    f" {self.state.global_step}! This is expected if you're using an IterableDataset and set"
                    f" num_steps ({max_steps}) higher than the number of available samples."
                )
                self.control.should_training_stop = True

            self.control = self.callback_handler.on_epoch_end(args, self.state, self.control)
            self._maybe_log_save_evaluate(tr_loss, None, model, trial, epoch, ignore_keys_for_eval)


            if self.control.should_training_stop:
                break

        if args.past_index and hasattr(self, "_past"):
            # Clean the state at the end of training
            delattr(self, "_past")

        logger.info("\n\nTraining completed. Do not forget to share your model on huggingface.co/models =)\n\n")
        if args.load_best_model_at_end and self.state.best_model_checkpoint is not None:

            self._load_best_model()

        # add remaining tr_loss
        self._total_loss_scalar += tr_loss.item()
        train_loss = self._total_loss_scalar / self.state.global_step

        metrics = speed_metrics("train", start_time, num_samples=num_train_samples, num_steps=self.state.max_steps)
        self.store_flos()
        metrics["total_flos"] = self.state.total_flos
        metrics["train_loss"] = train_loss

        self.is_in_train = False

        self._memory_tracker.stop_and_update_metrics(metrics)

        self.log(metrics)

        run_dir = self._get_output_dir(trial)
        checkpoints_sorted = self._sorted_checkpoints(use_mtime=False, output_dir=run_dir)

        # Delete the last checkpoint when save_total_limit=1 if it's different from the best checkpoint and process allowed to save.
        if self.args.should_save and self.state.best_model_checkpoint is not None and self.args.save_total_limit == 1:
            for checkpoint in checkpoints_sorted:
                if checkpoint != self.state.best_model_checkpoint:
                    logger.info(f"Deleting older checkpoint [{checkpoint}] due to args.save_total_limit")
                    shutil.rmtree(checkpoint)

        self.control = self.callback_handler.on_train_end(args, self.state, self.control)

        return TrainOutput(self.state.global_step, train_loss, metrics)
