# Copyright 2024 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
#
# Modified from diffusers==0.29.2
#
# ==============================================================================
import inspect
from typing import Any, Callable, Dict, List, Optional, Union, Tuple
import torch
import torch.nn as nn
import numpy as np
from dataclasses import dataclass
from packaging import version

from diffusers.callbacks import MultiPipelineCallbacks, PipelineCallback
from diffusers.configuration_utils import FrozenDict
from diffusers.image_processor import VaeImageProcessor
from diffusers.loaders import LoraLoaderMixin, TextualInversionLoaderMixin
from diffusers.models import AutoencoderKL
from diffusers.models.lora import adjust_lora_scale_text_encoder
from diffusers.schedulers import KarrasDiffusionSchedulers
from diffusers.utils import (
    USE_PEFT_BACKEND,
    deprecate,
    logging,
    replace_example_docstring,
    scale_lora_layers,
    unscale_lora_layers,
)
from diffusers.utils.torch_utils import randn_tensor
from diffusers.pipelines.pipeline_utils import DiffusionPipeline
from diffusers.utils import BaseOutput
from diffusers.loaders import (
    LoraLoaderMixin,
)
from matrixgame.vae_variants.matrixgame_vae_src import AutoencoderKLCausal3D
from matrixgame.encoder_variants.matrixgame_i2v import TextEncoder
from matrixgame.model_variants import MGVideoDiffusionTransformerI2V
from matrixgame.utils.utils import numpy_to_pil, black_image
from diffusers.video_processor import VideoProcessor

logger = logging.get_logger(__name__)  # pylint: disable=invalid-name

EXAMPLE_DOC_STRING = """"""

PRECISION_TO_TYPE = {
    'fp32': torch.float32,
    'fp16': torch.float16,
    'bf16': torch.bfloat16,
}

def rescale_noise_cfg(noise_cfg, noise_pred_text, guidance_rescale=0.0):
    """
    Rescale `noise_cfg` according to `guidance_rescale`. Based on findings of [Common Diffusion Noise Schedules and
    Sample Steps are Flawed](https://arxiv.org/pdf/2305.08891.pdf). See Section 3.4
    """
    std_text = noise_pred_text.std(
        dim=list(range(1, noise_pred_text.ndim)), keepdim=True
    )
    std_cfg = noise_cfg.std(dim=list(range(1, noise_cfg.ndim)), keepdim=True)
    # rescale the results from guidance (fixes overexposure)
    noise_pred_rescaled = noise_cfg * (std_text / std_cfg)
    # mix with the original results from guidance by factor guidance_rescale to avoid "plain looking" images
    noise_cfg = (
        guidance_rescale * noise_pred_rescaled + (1 - guidance_rescale) * noise_cfg
    )
    return noise_cfg


def retrieve_timesteps(
    scheduler,
    num_inference_steps: Optional[int] = None,
    device: Optional[Union[str, torch.device]] = None,
    timesteps: Optional[List[int]] = None,
    sigmas: Optional[List[float]] = None,
    **kwargs,
):
    """
    Calls the scheduler's `set_timesteps` method and retrieves timesteps from the scheduler after the call. Handles
    custom timesteps. Any kwargs will be supplied to `scheduler.set_timesteps`.

    Args:
        scheduler (`SchedulerMixin`):
            The scheduler to get timesteps from.
        num_inference_steps (`int`):
            The number of diffusion steps used when generating samples with a pre-trained model. If used, `timesteps`
            must be `None`.
        device (`str` or `torch.device`, *optional*):
            The device to which the timesteps should be moved to. If `None`, the timesteps are not moved.
        timesteps (`List[int]`, *optional*):
            Custom timesteps used to override the timestep spacing strategy of the scheduler. If `timesteps` is passed,
            `num_inference_steps` and `sigmas` must be `None`.
        sigmas (`List[float]`, *optional*):
            Custom sigmas used to override the timestep spacing strategy of the scheduler. If `sigmas` is passed,
            `num_inference_steps` and `timesteps` must be `None`.

    Returns:
        `Tuple[torch.Tensor, int]`: A tuple where the first element is the timestep schedule from the scheduler and the
        second element is the number of inference steps.
    """
    if timesteps is not None and sigmas is not None:
        raise ValueError(
            "Only one of `timesteps` or `sigmas` can be passed. Please choose one to set custom values"
        )
    if timesteps is not None:
        accepts_timesteps = "timesteps" in set(
            inspect.signature(scheduler.set_timesteps).parameters.keys()
        )
        if not accepts_timesteps:
            raise ValueError(
                f"The current scheduler class {scheduler.__class__}'s `set_timesteps` does not support custom"
                f" timestep schedules. Please check whether you are using the correct scheduler."
            )
        scheduler.set_timesteps(timesteps=timesteps, device=device, **kwargs)
        timesteps = scheduler.timesteps
        num_inference_steps = len(timesteps)
    elif sigmas is not None:
        accept_sigmas = "sigmas" in set(
            inspect.signature(scheduler.set_timesteps).parameters.keys()
        )
        if not accept_sigmas:
            raise ValueError(
                f"The current scheduler class {scheduler.__class__}'s `set_timesteps` does not support custom"
                f" sigmas schedules. Please check whether you are using the correct scheduler."
            )
        scheduler.set_timesteps(sigmas=sigmas, device=device, **kwargs)
        timesteps = scheduler.timesteps
        num_inference_steps = len(timesteps)
    else:
        scheduler.set_timesteps(num_inference_steps, device=device, **kwargs)
        timesteps = scheduler.timesteps
    return timesteps, num_inference_steps


@dataclass
class MatrixGameVideoPipelineOutput(BaseOutput):
    videos: Union[torch.Tensor, np.ndarray]


class MatrixGameVideoPipeline(DiffusionPipeline):
    r"""
    Pipeline for text-to-video generation using MatrixGameVideo.

    This model inherits from [`DiffusionPipeline`]. Check the superclass documentation for the generic methods
    implemented for all pipelines (downloading, saving, running on a particular device, etc.).

    Args:
        vae ([`AutoencoderKL`]):
            Variational Auto-Encoder (VAE) model to encode and decode images to and from latent representations.
        text_encoder ([`TextEncoder`]):
            Frozen text-encoder.
        text_encoder_2 ([`TextEncoder`]):
            Frozen text-encoder_2.
        transformer ([`MGVideoDiffusionTransformerI2V`]):
            A `MGVideoDiffusionTransformerI2V` to denoise the encoded video latents.
        scheduler ([`SchedulerMixin`]):
            A scheduler to be used in combination with `unet` to denoise the encoded image latents.
    """

    model_cpu_offload_seq = "text_encoder->text_encoder_2->transformer->vae"
    _optional_components = ["text_encoder_2"]
    _exclude_from_cpu_offload = ["transformer"]
    _callback_tensor_inputs = ["latents", "prompt_embeds", "negative_prompt_embeds"]

    def __init__(
        self,
        vae: AutoencoderKL,
        text_encoder: TextEncoder,
        transformer: MGVideoDiffusionTransformerI2V,
        scheduler: KarrasDiffusionSchedulers,
    ):
        super().__init__()

        # ==========================================================================================
        
        progress_bar_config = {}
        if not hasattr(self, "_progress_bar_config"):
            self._progress_bar_config = {}
        self._progress_bar_config.update(progress_bar_config)

        # ==========================================================================================

        if (
            hasattr(scheduler.config, "steps_offset")
            and scheduler.config.steps_offset != 1
        ):
            deprecation_message = (
                f"The configuration file of this scheduler: {scheduler} is outdated. `steps_offset`"
                f" should be set to 1 instead of {scheduler.config.steps_offset}. Please make sure "
                "to update the config accordingly as leaving `steps_offset` might led to incorrect results"
                " in future versions. If you have downloaded this checkpoint from the Hugging Face Hub,"
                " it would be very nice if you could open a Pull request for the `scheduler/scheduler_config.json`"
                " file"
            )
            deprecate(
                "steps_offset!=1", "1.0.0", deprecation_message, standard_warn=False
            )
            new_config = dict(scheduler.config)
            new_config["steps_offset"] = 1
            scheduler._internal_dict = FrozenDict(new_config)

        if (
            hasattr(scheduler.config, "clip_sample")
            and scheduler.config.clip_sample is True
        ):
            deprecation_message = (
                f"The configuration file of this scheduler: {scheduler} has not set the configuration `clip_sample`."
                " `clip_sample` should be set to False in the configuration file. Please make sure to update the"
                " config accordingly as not setting `clip_sample` in the config might lead to incorrect results in"
                " future versions. If you have downloaded this checkpoint from the Hugging Face Hub, it would be very"
                " nice if you could open a Pull request for the `scheduler/scheduler_config.json` file"
            )
            deprecate(
                "clip_sample not set", "1.0.0", deprecation_message, standard_warn=False
            )
            new_config = dict(scheduler.config)
            new_config["clip_sample"] = False
            scheduler._internal_dict = FrozenDict(new_config)

        self.register_modules(
            vae=vae,
            text_encoder=text_encoder,
            transformer=transformer,
            scheduler=scheduler
        )
        self.vae_scale_factor = 2 ** (len(self.vae.config.block_out_channels) - 1)
        self.image_processor = VaeImageProcessor(vae_scale_factor=self.vae_scale_factor)

        self.video_processor = VideoProcessor(vae_scale_factor=self.vae_scale_factor)


    def encode_prompt(
        self,
        prompt,
        device,
        num_videos_per_prompt,
        do_classifier_free_guidance,
        negative_prompt=None,
        prompt_embeds: Optional[torch.Tensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        negative_prompt_embeds: Optional[torch.Tensor] = None,
        negative_attention_mask: Optional[torch.Tensor] = None,
        lora_scale: Optional[float] = None,
        clip_skip: Optional[int] = None,
        text_encoder: Optional[TextEncoder] = None,
        data_type: Optional[str] = "image",
        semantic_images=None
    ):
        r"""
        Encodes the prompt into text encoder hidden states.

        Args:
            prompt (`str` or `List[str]`, *optional*):
                prompt to be encoded
            device: (`torch.device`):
                torch device
            num_videos_per_prompt (`int`):
                number of videos that should be generated per prompt
            do_classifier_free_guidance (`bool`):
                whether to use classifier free guidance or not
            negative_prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts not to guide the video generation. If not defined, one has to pass
                `negative_prompt_embeds` instead. Ignored when not using guidance (i.e., ignored if `guidance_scale` is
                less than `1`).
            prompt_embeds (`torch.Tensor`, *optional*):
                Pre-generated text embeddings. Can be used to easily tweak text inputs, *e.g.* prompt weighting. If not
                provided, text embeddings will be generated from `prompt` input argument.
            attention_mask (`torch.Tensor`, *optional*):
            negative_prompt_embeds (`torch.Tensor`, *optional*):
                Pre-generated negative text embeddings. Can be used to easily tweak text inputs, *e.g.* prompt
                weighting. If not provided, negative_prompt_embeds will be generated from `negative_prompt` input
                argument.
            negative_attention_mask (`torch.Tensor`, *optional*):
            lora_scale (`float`, *optional*):
                A LoRA scale that will be applied to all LoRA layers of the text encoder if LoRA layers are loaded.
            clip_skip (`int`, *optional*):
                Number of layers to be skipped from CLIP while computing the prompt embeddings. A value of 1 means that
                the output of the pre-final layer will be used for computing the prompt embeddings.
            text_encoder (TextEncoder, *optional*):
            data_type (`str`, *optional*):
        """
        text_encoder = self.text_encoder
        if not isinstance(semantic_images, list):
            semantic_images = [semantic_images]
        uncond_image = [black_image(img.size[0], img.size[1]) for img in semantic_images]
        #uncond_image = semantic_images
        prompt_embeds, attention_mask, _ = text_encoder(prompt, device = device, semantic_images = semantic_images)
        negative_prompt_embeds, negative_attention_mask, _ = text_encoder(negative_prompt, device = device, semantic_images = uncond_image)

        return (
            prompt_embeds,
            negative_prompt_embeds,
            attention_mask,
            negative_attention_mask,
        )

    def decode_latents(self, latents, enable_tiling=True):
        deprecation_message = "The decode_latents method is deprecated and will be removed in 1.0.0. Please use VaeImageProcessor.postprocess(...) instead"
        deprecate("decode_latents", "1.0.0", deprecation_message, standard_warn=False)

        image = self.vae.decode(latents)
        image = (image / 2 + 0.5).clamp(0, 1)
        # we always cast to float32 as this does not cause significant overhead and is compatible with bfloat16
        if image.ndim == 4:
            image = image.cpu().permute(0, 2, 3, 1).float()
        else:
            image = image.cpu().float()
        return image

    def prepare_extra_func_kwargs(self, func, kwargs):
        # prepare extra kwargs for the scheduler step, since not all schedulers have the same signature
        # eta (η) is only used with the DDIMScheduler, it will be ignored for other schedulers.
        # eta corresponds to η in DDIM paper: https://arxiv.org/abs/2010.02502
        # and should be between [0, 1]
        extra_step_kwargs = {}

        for k, v in kwargs.items():
            accepts = k in set(inspect.signature(func).parameters.keys())
            if accepts:
                extra_step_kwargs[k] = v
        return extra_step_kwargs

    def check_inputs(
        self,
        prompt,
        height,
        width,
        video_length,
        callback_steps,
        negative_prompt=None,
        prompt_embeds=None,
        negative_prompt_embeds=None,
        callback_on_step_end_tensor_inputs=None,
        vae_ver="88-4c-sd",
    ):
        if height % 8 != 0 or width % 8 != 0:
            raise ValueError(
                f"`height` and `width` have to be divisible by 8 but are {height} and {width}."
            )

        if video_length is not None:
            if "884" in vae_ver:
                if video_length != 1 and (video_length - 1) % 4 != 0:
                    raise ValueError(
                        f"`video_length` has to be 1 or a multiple of 4 but is {video_length}."
                    )
            elif "888" in vae_ver:
                if video_length != 1 and (video_length - 1) % 8 != 0:
                    raise ValueError(
                        f"`video_length` has to be 1 or a multiple of 8 but is {video_length}."
                    )

        if callback_steps is not None and (
            not isinstance(callback_steps, int) or callback_steps <= 0
        ):
            raise ValueError(
                f"`callback_steps` has to be a positive integer but is {callback_steps} of type"
                f" {type(callback_steps)}."
            )
        if callback_on_step_end_tensor_inputs is not None and not all(
            k in self._callback_tensor_inputs
            for k in callback_on_step_end_tensor_inputs
        ):
            raise ValueError(
                f"`callback_on_step_end_tensor_inputs` has to be in {self._callback_tensor_inputs}, but found {[k for k in callback_on_step_end_tensor_inputs if k not in self._callback_tensor_inputs]}"
            )

        if prompt is not None and prompt_embeds is not None:
            raise ValueError(
                f"Cannot forward both `prompt`: {prompt} and `prompt_embeds`: {prompt_embeds}. Please make sure to"
                " only forward one of the two."
            )
        elif prompt is None and prompt_embeds is None:
            raise ValueError(
                "Provide either `prompt` or `prompt_embeds`. Cannot leave both `prompt` and `prompt_embeds` undefined."
            )
        elif prompt is not None and (
            not isinstance(prompt, str) and not isinstance(prompt, list)
        ):
            raise ValueError(
                f"`prompt` has to be of type `str` or `list` but is {type(prompt)}"
            )

        if negative_prompt is not None and negative_prompt_embeds is not None:
            raise ValueError(
                f"Cannot forward both `negative_prompt`: {negative_prompt} and `negative_prompt_embeds`:"
                f" {negative_prompt_embeds}. Please make sure to only forward one of the two."
            )

        if prompt_embeds is not None and negative_prompt_embeds is not None:
            if prompt_embeds.shape != negative_prompt_embeds.shape:
                raise ValueError(
                    "`prompt_embeds` and `negative_prompt_embeds` must have the same shape when passed directly, but"
                    f" got: `prompt_embeds` {prompt_embeds.shape} != `negative_prompt_embeds`"
                    f" {negative_prompt_embeds.shape}."
                )


    def prepare_latents(
        self,
        initial_image,
        batch_size,
        num_channels_latents,
        height,
        width,
        video_length,
        dtype,
        device,
        generator, 
        latents=None,
        i2v_type = 'refiner'
    ):
        shape = (
            batch_size,
            num_channels_latents,
            video_length,
            int(height) // self.vae_scale_factor,
            int(width) // self.vae_scale_factor,
        )
        if isinstance(generator, list) and len(generator) != batch_size:
            raise ValueError(
                f"You have passed a list of generators of length {len(generator)}, but requested an effective batch"
                f" size of {batch_size}. Make sure the batch size matches the length of the generators."
            )

        if latents is None:
            latents = randn_tensor(
                shape, generator=generator, device=device, dtype=dtype
            )
        else:
            latents = latents.to(device)

        # Check existence to make it compatible with FlowMatchEulerDiscreteScheduler
        if hasattr(self.scheduler, "init_noise_sigma"):
            # scale the initial noise by the standard deviation required by the scheduler
            latents = latents * self.scheduler.init_noise_sigma

        if initial_image is not None:
            if initial_image.shape[0] > 1:
                initial_image = initial_image.unsqueeze(0).permute(0, 2, 1, 3, 4)
            else:
                initial_image = initial_image.unsqueeze(2)
            if self.args and self.args.num_pre_frames >0: 
                past_frames = initial_image[:,:,1:]
                initial_image = initial_image[:,:,:1]

                image_latents = self.vae.encode(initial_image).latent_dist.sample()
                past_frames_latents = self.vae.encode(past_frames).latent_dist.sample()
                image_latents = torch.cat([image_latents, past_frames_latents], dim=2)
            else:
                image_latents = self.vae.encode(initial_image).latent_dist.sample()
            num_frames = image_latents.shape[2]
            if hasattr(self.vae.config, "shift_factor") and self.vae.config.shift_factor:
                image_latents = (image_latents - self.vae.config.shift_factor) * self.vae.config.scaling_factor
            else:
                image_latents = image_latents * self.vae.config.scaling_factor
            x1 = image_latents[:,:,:1,...].repeat(1, 1, video_length, 1, 1)
            x0 = latents
            t = torch.tensor([0.999]).to(device=device)
            latents = x0 * t + x1 * (1 - t)
            latents = latents.to(dtype=dtype)


            #

            if i2v_type == 'concat':
                padding_shape = (
                    batch_size,
                    num_channels_latents,
                    video_length-image_latents.shape[2],
                    int(height) // self.vae_scale_factor,
                    int(width) // self.vae_scale_factor,
                )
                latent_padding = torch.zeros(padding_shape, device=device, dtype=dtype)
                image_latents = torch.cat([image_latents, latent_padding], dim=2)
                mask_concat = torch.ones(batch_size, 1, image_latents.shape[2], image_latents.shape[3], image_latents.shape[4]).to(device=image_latents.device, dtype=image_latents.dtype)
                mask_concat[:, :, num_frames:, ...] = 0.0
                image_latents = torch.cat([image_latents, mask_concat], dim=1)
            elif i2v_type == 'refiner':
                image_latents = image_latents
                if self.args.num_pre_frames > 0:
                    past_frame_latents = image_latents[:,:,1:,...]

                    num_pre_frames = past_frames_latents.shape[2]
                    image_latents = image_latents[:,:,:1,...]
                    padding_shape = (batch_size, num_channels_latents, video_length-num_pre_frames, latents.shape[3], latents.shape[4])     
                    latent_padding = image_latents.new_zeros(padding_shape)
                    concat_latents = torch.cat([past_frame_latents, latent_padding], dim=2)
                    mask_concat = torch.ones(latents.shape[0], 1, latents.shape[2], latents.shape[3], latents.shape[4]).to(device=latents.device, dtype=latents.dtype)
                    mask_concat[:, :, num_pre_frames:, ...] = 0.0
                    concat_latents = torch.cat([concat_latents, mask_concat], dim=1) 
        else:
            image_latents = None
        # Check existence to make it compatible with FlowMatchEulerDiscreteScheduler
        if hasattr(self.scheduler, "init_noise_sigma"):
            # scale the initial noise by the standard deviation required by the scheduler
            latents = latents * self.scheduler.init_noise_sigma

        if self.args.num_pre_frames > 0:
            return latents, image_latents, concat_latents
        else:
            return latents, image_latents


    # Copied from diffusers.pipelines.latent_consistency_models.pipeline_latent_consistency_text2img.LatentConsistencyModelPipeline.get_guidance_scale_embedding
    def get_guidance_scale_embedding(
        self,
        w: torch.Tensor,
        embedding_dim: int = 512,
        dtype: torch.dtype = torch.float32,
    ) -> torch.Tensor:
        """
        See https://github.com/google-research/vdm/blob/dc27b98a554f65cdc654b800da5aa1846545d41b/model_vdm.py#L298

        Args:
            w (`torch.Tensor`):
                Generate embedding vectors with a specified guidance scale to subsequently enrich timestep embeddings.
            embedding_dim (`int`, *optional*, defaults to 512):
                Dimension of the embeddings to generate.
            dtype (`torch.dtype`, *optional*, defaults to `torch.float32`):
                Data type of the generated embeddings.

        Returns:
            `torch.Tensor`: Embedding vectors with shape `(len(w), embedding_dim)`.
        """
        assert len(w.shape) == 1
        w = w * 1000.0

        half_dim = embedding_dim // 2
        emb = torch.log(torch.tensor(10000.0)) / (half_dim - 1)
        emb = torch.exp(torch.arange(half_dim, dtype=dtype) * -emb)
        emb = w.to(dtype)[:, None] * emb[None, :]
        emb = torch.cat([torch.sin(emb), torch.cos(emb)], dim=1)
        if embedding_dim % 2 == 1:  # zero pad
            emb = torch.nn.functional.pad(emb, (0, 1))
        assert emb.shape == (w.shape[0], embedding_dim)
        return emb

    @property
    def guidance_scale(self):
        return self._guidance_scale

    @property
    def guidance_rescale(self):
        return self._guidance_rescale

    @property
    def clip_skip(self):
        return self._clip_skip

    # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
    # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
    # corresponds to doing no classifier free guidance.
    @property
    def do_classifier_free_guidance(self):
        # return self._guidance_scale > 1 and self.transformer.config.time_cond_proj_dim is None
        return self._guidance_scale > 1

    @property
    def cross_attention_kwargs(self):
        return self._cross_attention_kwargs

    @property
    def num_timesteps(self):
        return self._num_timesteps

    @property
    def interrupt(self):
        return self._interrupt

    @torch.no_grad()
    @replace_example_docstring(EXAMPLE_DOC_STRING)
    def __call__(
        self,
        height: int,
        width: int,
        video_length: int,
        initial_image = None,
        mouse_condition = None,
        keyboard_condition = None,
        data_type: str = "video",
        num_inference_steps: int = 50,
        timesteps: List[int] = None,
        sigmas: List[float] = None,
        guidance_scale: float = 7.5,
        num_videos_per_prompt: Optional[int] = 1,
        eta: float = 0.0,
        generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
        latents: Optional[torch.Tensor] = None,
        prompt_embeds: Optional[torch.Tensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        negative_prompt_embeds: Optional[torch.Tensor] = None,
        negative_attention_mask: Optional[torch.Tensor] = None,
        output_type: Optional[str] = "pil",
        return_dict: bool = True,
        cross_attention_kwargs: Optional[Dict[str, Any]] = None,
        guidance_rescale: float = 0.0,
        clip_skip: Optional[int] = None,
        callback_on_step_end: Optional[
            Union[
                Callable[[int, int, Dict], None],
                PipelineCallback,
                MultiPipelineCallbacks,
            ]
        ] = None,
        callback_on_step_end_tensor_inputs: List[str] = ["latents"],
        freqs_cis: Tuple[torch.Tensor, torch.Tensor] = None,
        vae_ver: str = "88-4c-sd",
        enable_tiling: bool = False,
        embedded_guidance_scale: Optional[float] = None,
        i2v_type = 'refiner',
        args = None, 
        semantic_images=None,
        prompt = "N.o.n.e",
        negative_prompt = ['N.o.n.e'],
        **kwargs,
    ):
        r"""
        The call function to the pipeline for generation.

        Args:
            prompt (`str` or `List[str]`):
                The prompt or prompts to guide image generation. If not defined, you need to pass `prompt_embeds`.
            height (`int`):
                The height in pixels of the generated image.
            width (`int`):
                The width in pixels of the generated image.
            video_length (`int`):
                The number of frames in the generated video.
            num_inference_steps (`int`, *optional*, defaults to 50):
                The number of denoising steps. More denoising steps usually lead to a higher quality image at the
                expense of slower inference.
            timesteps (`List[int]`, *optional*):
                Custom timesteps to use for the denoising process with schedulers which support a `timesteps` argument
                in their `set_timesteps` method. If not defined, the default behavior when `num_inference_steps` is
                passed will be used. Must be in descending order.
            sigmas (`List[float]`, *optional*):
                Custom sigmas to use for the denoising process with schedulers which support a `sigmas` argument in
                their `set_timesteps` method. If not defined, the default behavior when `num_inference_steps` is passed
                will be used.
            guidance_scale (`float`, *optional*, defaults to 7.5):
                A higher guidance scale value encourages the model to generate images closely linked to the text
                `prompt` at the expense of lower image quality. Guidance scale is enabled when `guidance_scale > 1`.
            negative_prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts to guide what to not include in image generation. If not defined, you need to
                pass `negative_prompt_embeds` instead. Ignored when not using guidance (`guidance_scale < 1`).
            num_videos_per_prompt (`int`, *optional*, defaults to 1):
                The number of images to generate per prompt.
            eta (`float`, *optional*, defaults to 0.0):
                Corresponds to parameter eta (η) from the [DDIM](https://arxiv.org/abs/2010.02502) paper. Only applies
                to the [`~schedulers.DDIMScheduler`], and is ignored in other schedulers.
            generator (`torch.Generator` or `List[torch.Generator]`, *optional*):
                A [`torch.Generator`](https://pytorch.org/docs/stable/generated/torch.Generator.html) to make
                generation deterministic.
            latents (`torch.Tensor`, *optional*):
                Pre-generated noisy latents sampled from a Gaussian distribution, to be used as inputs for image
                generation. Can be used to tweak the same generation with different prompts. If not provided, a latents
                tensor is generated by sampling using the supplied random `generator`.
            prompt_embeds (`torch.Tensor`, *optional*):
                Pre-generated text embeddings. Can be used to easily tweak text inputs (prompt weighting). If not
                provided, text embeddings are generated from the `prompt` input argument.
            negative_prompt_embeds (`torch.Tensor`, *optional*):
                Pre-generated negative text embeddings. Can be used to easily tweak text inputs (prompt weighting). If
                not provided, `negative_prompt_embeds` are generated from the `negative_prompt` input argument.
                
            output_type (`str`, *optional*, defaults to `"pil"`):
                The output format of the generated image. Choose between `PIL.Image` or `np.array`.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`MatrixGameVideoPipelineOutput`] instead of a
                plain tuple.
            cross_attention_kwargs (`dict`, *optional*):
                A kwargs dictionary that if specified is passed along to the [`AttentionProcessor`] as defined in
                [`self.processor`](https://github.com/huggingface/diffusers/blob/main/src/diffusers/models/attention_processor.py).
            guidance_rescale (`float`, *optional*, defaults to 0.0):
                Guidance rescale factor from [Common Diffusion Noise Schedules and Sample Steps are
                Flawed](https://arxiv.org/pdf/2305.08891.pdf). Guidance rescale factor should fix overexposure when
                using zero terminal SNR.
            clip_skip (`int`, *optional*):
                Number of layers to be skipped from CLIP while computing the prompt embeddings. A value of 1 means that
                the output of the pre-final layer will be used for computing the prompt embeddings.
            callback_on_step_end (`Callable`, `PipelineCallback`, `MultiPipelineCallbacks`, *optional*):
                A function or a subclass of `PipelineCallback` or `MultiPipelineCallbacks` that is called at the end of
                each denoising step during the inference. with the following arguments: `callback_on_step_end(self:
                DiffusionPipeline, step: int, timestep: int, callback_kwargs: Dict)`. `callback_kwargs` will include a
                list of all tensors as specified by `callback_on_step_end_tensor_inputs`.
            callback_on_step_end_tensor_inputs (`List`, *optional*):
                The list of tensor inputs for the `callback_on_step_end` function. The tensors specified in the list
                will be passed as `callback_kwargs` argument. You will only be able to include variables listed in the
                `._callback_tensor_inputs` attribute of your pipeline class.

        Examples:

        Returns:
            [`~MatrixGameVideoPipelineOutput`] or `tuple`:
                If `return_dict` is `True`, [`MatrixGameVideoPipelineOutput`] is returned,
                otherwise a `tuple` is returned where the first element is a list with the generated images and the
                second element is a list of `bool`s indicating whether the corresponding generated image contains
                "not-safe-for-work" (nsfw) content.
        """
        callback = kwargs.pop("callback", None)
        callback_steps = kwargs.pop("callback_steps", None)

        if callback is not None:
            deprecate(
                "callback",
                "1.0.0",
                "Passing `callback` as an input argument to `__call__` is deprecated, consider using `callback_on_step_end`",
            )
        if callback_steps is not None:
            deprecate(
                "callback_steps",
                "1.0.0",
                "Passing `callback_steps` as an input argument to `__call__` is deprecated, consider using `callback_on_step_end`",
            )

        if isinstance(callback_on_step_end, (PipelineCallback, MultiPipelineCallbacks)):
            callback_on_step_end_tensor_inputs = callback_on_step_end.tensor_inputs

        # 0. Default height and width to unet
        # height = height or self.transformer.config.sample_size * self.vae_scale_factor
        # width = width or self.transformer.config.sample_size * self.vae_scale_factor
        # to deal with lora scaling and other possible forward hooks

        # 1. Check inputs. Raise error if not correct
        self.check_inputs(
            prompt,
            height,
            width,
            video_length,
            callback_steps,
            negative_prompt,
            prompt_embeds,
            negative_prompt_embeds,
            callback_on_step_end_tensor_inputs,
            vae_ver=vae_ver,
        )

        self._guidance_scale = guidance_scale
        self._guidance_rescale = guidance_rescale
        self._clip_skip = clip_skip
        self._cross_attention_kwargs = cross_attention_kwargs
        self._interrupt = False
        self.args = args

        # 2. Define call parameters
        if prompt is not None and isinstance(prompt, str):
            batch_size = 1
        elif prompt is not None and isinstance(prompt, list):
            batch_size = len(prompt)
        else:
            batch_size = prompt_embeds.shape[0]

        device = self._execution_device

        # 3. Encode input prompt
        lora_scale = (
            self.cross_attention_kwargs.get("scale", None)
            if self.cross_attention_kwargs is not None
            else None
        )
        n_tokens = (height//16 * width // 16 * ((video_length-1)//4+1))

        if semantic_images is None:
            if args.num_pre_frames > 0: 
                semantic_images = initial_image[:1,...] #semantic_images torch.Size([1, 3, 352, 640])  #
                # print("semantic_images", semantic_images.shape)   
                semantic_images = (semantic_images / 2 + 0.5).clamp(0, 1).cpu().permute(0, 2, 3, 1).float().numpy() # b c h w -> b h w c
                semantic_images = numpy_to_pil(semantic_images)
            else:
                semantic_images = initial_image

        (
            prompt_embeds,
            negative_prompt_embeds,
            prompt_mask,
            negative_prompt_mask,
        ) = self.encode_prompt(
            prompt,
            device,
            num_videos_per_prompt,
            self.do_classifier_free_guidance,
            negative_prompt,
            prompt_embeds=prompt_embeds,
            attention_mask=attention_mask,
            negative_prompt_embeds=negative_prompt_embeds,
            negative_attention_mask=negative_attention_mask,
            lora_scale=lora_scale,
            clip_skip=self.clip_skip,
            data_type=data_type,
            semantic_images=semantic_images
        )
        prompt_embeds, prompt_embeds_2 = prompt_embeds
        negative_prompt_embeds, negative_prompt_embeds_2 = negative_prompt_embeds
        prompt_mask, prompt_mask_2 = prompt_mask
        negative_prompt_mask, negative_prompt_mask_2 = negative_prompt_mask
        prompt_embeds  = prompt_embeds[:,:144, ...]  # 144 is the number of img tokens after MLLM
        prompt_mask  = prompt_mask[:,:144, ...]
        negative_prompt_embeds = negative_prompt_embeds[:,:144, ...]
        negative_prompt_mask = negative_prompt_mask[:,:144, ...] 

        # For classifier free guidance, we need to do two forward passes.
        # Here we concatenate the unconditional and text embeddings into a single batch
        # to avoid doing two forward passes
        if self.do_classifier_free_guidance:
            prompt_embeds = torch.cat([negative_prompt_embeds, prompt_embeds])
            if prompt_mask is not None:
                prompt_mask = torch.cat([negative_prompt_mask, prompt_mask])
            if prompt_embeds_2 is not None:
                prompt_embeds_2 = torch.cat([negative_prompt_embeds_2, prompt_embeds_2])
            if prompt_mask_2 is not None:
                prompt_mask_2 = torch.cat([negative_prompt_mask_2, prompt_mask_2])


        # 4. Prepare timesteps
        extra_set_timesteps_kwargs = self.prepare_extra_func_kwargs(
            self.scheduler.set_timesteps, {"n_tokens": n_tokens}
        )
        timesteps, num_inference_steps = retrieve_timesteps(
            self.scheduler,
            num_inference_steps,
            device,
            timesteps,
            sigmas,
            **extra_set_timesteps_kwargs,
        )

        if "884" in vae_ver:
            video_length = (video_length - 1) // 4 + 1
        elif "888" in vae_ver:
            video_length = (video_length - 1) // 8 + 1
        else:
            video_length = video_length


        if initial_image is not None:
            initial_image = initial_image.to(
                device, dtype=prompt_embeds.dtype
            )
            if i2v_type == 'concat':
                num_channels_latents = self.transformer.config.in_channels // 2
            else:
                num_channels_latents = self.transformer.config.in_channels // 2
        else:
            num_channels_latents = self.transformer.config.in_channels

        # 5. Prepare latent variables
        if args.num_pre_frames > 0:
            latents, image_latents, concat_latents = self.prepare_latents(
            initial_image,
            batch_size * num_videos_per_prompt,
            num_channels_latents,
            height,
            width,
            video_length,
            prompt_embeds.dtype,
            device,
            generator,
            latents,
            i2v_type, 
        )
        else: 
            latents, image_latents = self.prepare_latents(
            initial_image,
            batch_size * num_videos_per_prompt,
            num_channels_latents,
            height,
            width,
            video_length,
            prompt_embeds.dtype,
            device,
            generator,
            latents,
            i2v_type, 
        )

        # 6. Prepare extra step kwargs. TODO: Logic should ideally just be moved out of the pipeline
        extra_step_kwargs = self.prepare_extra_func_kwargs(
            self.scheduler.step,
            {"generator": generator, "eta": eta},
        )

        target_dtype = self.dtype
        autocast_enabled = target_dtype != torch.float32
        vae_dtype = self.dtype
        vae_autocast_enabled = vae_dtype != torch.float32

        # 7. Denoising loop
        num_warmup_steps = len(timesteps) - num_inference_steps * self.scheduler.order
        self._num_timesteps = len(timesteps)
        if args.num_pre_frames > 0:      
            concat_latents = (
                torch.cat([torch.zeros_like(concat_latents), concat_latents]) 
                if self.do_classifier_free_guidance
                else concat_latents
            )        
        # if is_progress_bar:
        with self.progress_bar(total=num_inference_steps) as progress_bar:
            for i, t in enumerate(timesteps):
                if self.interrupt:
                    continue

                # expand the latents if we are doing classifier free guidance
                latent_model_input = (
                    torch.cat([latents] * 2)
                    if self.do_classifier_free_guidance
                    else latents
                )                
                latent_model_input = self.scheduler.scale_model_input( #do nothing
                    latent_model_input, t
                )
                if image_latents is not None:
                    latent_image_input = torch.cat([image_latents] * 2) if self.do_classifier_free_guidance else image_latents
                    if i2v_type == 'concat':
                        latent_model_input = torch.cat([latent_model_input, latent_image_input], dim=1)
                    else:
                        if args.num_pre_frames > 0:       
                            num_pre = latent_image_input.shape[2]
                            #print("latent_image_input", latent_image_input.shape)  #latent_image_input torch.Size([2, 16, 1, 44, 80])
                            #print("latent_model_input", latent_model_input.shape)   #latent_model_input torch.Size([2, 16, 5, 44, 80])
                            #print("concat_latents", concat_latents.shape)  #concat_latents torch.Size([1, 17, 5, 44, 80])
                            latent_model_input = torch.cat([latent_image_input, latent_model_input[:,:,num_pre:,...]], dim=2)

                            latent_model_input = torch.cat([latent_model_input, concat_latents], dim=1) 
                        else:
                            num_pre = latent_image_input.shape[2]   # 如果多帧past frame需要修改
                            latent_model_input = torch.cat([latent_image_input, latent_model_input[:,:,num_pre:,...]], dim=2)

                t_expand = t.repeat(latent_model_input.shape[0])
                guidance_expand = (
                    torch.tensor(
                        [embedded_guidance_scale] * latent_model_input.shape[0],
                        dtype=torch.float32,
                        device=device,
                    ).to(target_dtype)
                    * 1000.0
                    if embedded_guidance_scale is not None
                    else None
                )
                if mouse_condition is not None:
                    mouse_condition_input = (
                        torch.cat([torch.full_like(mouse_condition, 1.0), mouse_condition])
                        if self.do_classifier_free_guidance
                        else mouse_condition
                    )
                    # torch.zeros_like(mouse_condition)
                    # torch.full_like(mouse_condition, 999.0)
                    keyboard_condition_input = (
                        torch.cat([torch.full_like(keyboard_condition, -1.0), keyboard_condition])
                        if self.do_classifier_free_guidance
                        else keyboard_condition
                    )
                    #torch.full_like(keyboard_condition, -1.0)
                    #torch.zeros_like(keyboard_condition)
                    # mouse_condition = torch.full_like(mouse_condition, 999.0)
                    # keyboard 是 6维，改成 -1
                    # keyboard_condition = torch.full_like(keyboard_condition, -1.0)
                else:
                    mouse_condition_input = None
                    keyboard_condition_input = None
                _, _, ot, oh, ow = latent_model_input.shape
                freqs_cos, freqs_sin = self.transformer.get_rotary_pos_embed(ot, oh, ow)
                # predict the noise residual
                with torch.autocast(
                    device_type="cuda", dtype=target_dtype, enabled=autocast_enabled
                ):
                    # import ipdb;ipdb.set_trace()
                    noise_pred = self.transformer(  # For an input image (129, 192, 336) (1, 256, 256)
                        hidden_states = latent_model_input,  # [2, 16, 33, 24, 42]
                        timestep = t_expand,  # [2]
                        encoder_hidden_states=(prompt_embeds, prompt_embeds_2),  # [2, 256, 4096]
                        encoder_attention_mask=(prompt_mask, prompt_mask_2),  # [2, 256]
                        freqs_cos = freqs_cos,
                        freqs_sin = freqs_sin,
                        guidance=guidance_expand,
                        return_dict=True,
                        mouse_condition = mouse_condition_input,
                        keyboard_condition = keyboard_condition_input,
                    )["x"]

                # perform guidance
                if self.do_classifier_free_guidance:
                    noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
                    noise_pred = noise_pred_uncond + self.guidance_scale * (
                        noise_pred_text - noise_pred_uncond
                    )

                if self.do_classifier_free_guidance and self.guidance_rescale > 0.0:
                    # Based on 3.4. in https://arxiv.org/pdf/2305.08891.pdf
                    noise_pred = rescale_noise_cfg(
                        noise_pred,
                        noise_pred_text,
                        guidance_rescale=self.guidance_rescale,
                    )

                # compute the previous noisy sample x_t -> x_t-1
                if i2v_type == "concat":
                    latents = self.scheduler.step(
                        noise_pred, t, latents, **extra_step_kwargs, return_dict=False
                    )[0]
                else:
                    latents = self.scheduler.step(
                        noise_pred[:,:,1:,...], t, latents[:,:,1:,...], **extra_step_kwargs, return_dict=False
                    )[0]
                    latents = torch.concat(
                        [image_latents, latents], dim=2
                    )

                if callback_on_step_end is not None:
                    callback_kwargs = {}
                    for k in callback_on_step_end_tensor_inputs:
                        callback_kwargs[k] = locals()[k]
                    callback_outputs = callback_on_step_end(self, i, t, callback_kwargs)

                    latents = callback_outputs.pop("latents", latents)
                    prompt_embeds = callback_outputs.pop("prompt_embeds", prompt_embeds)
                    negative_prompt_embeds = callback_outputs.pop(
                        "negative_prompt_embeds", negative_prompt_embeds
                    )

                # call the callback, if provided
                if i == len(timesteps) - 1 or (
                    (i + 1) > num_warmup_steps and (i + 1) % self.scheduler.order == 0
                ):
                    if progress_bar is not None:
                        progress_bar.update()
                    if callback is not None and i % callback_steps == 0:
                        step_idx = i // getattr(self.scheduler, "order", 1)
                        callback(step_idx, t, latents)

        if not output_type == "latent":
            expand_temporal_dim = False
            if len(latents.shape) == 4:
                if isinstance(self.vae, AutoencoderKLCausal3D):
                    latents = latents.unsqueeze(2)
                    expand_temporal_dim = True
            elif len(latents.shape) == 5:
                pass
            else:
                raise ValueError(
                    f"Only support latents with shape (b, c, h, w) or (b, c, f, h, w), but got {latents.shape}."
                )

            if (
                hasattr(self.vae.config, "shift_factor")
                and self.vae.config.shift_factor
            ):
                latents = (
                    latents / self.vae.config.scaling_factor
                    + self.vae.config.shift_factor
                )
            else:
                latents = latents / self.vae.config.scaling_factor

            with torch.autocast(
                device_type="cuda", dtype=vae_dtype, enabled=vae_autocast_enabled
            ):
                if enable_tiling:
                    self.vae.enable_tiling()
                    image = self.vae.decode(
                        latents, return_dict=False, generator=generator
                    )[0]
                else:
                    image = self.vae.decode(
                        latents, return_dict=False, generator=generator
                    )[0]

            if expand_temporal_dim or image.shape[2] == 1:
                image = image.squeeze(2)

        else:
            image = latents

        image = (image / 2 + 0.5).clamp(0, 1)
        # we always cast to float32 as this does not cause significant overhead and is compatible with bfloa16
        image = image.cpu().float()
        # if i2v_type == "concat":
        #     image = image[:,:,4:,...]
        # Offload all models
        self.maybe_free_model_hooks()

        if not return_dict:
            return image

        return MatrixGameVideoPipelineOutput(videos=image)
