# Open WebUI integration

This directory contains an optional stock Open WebUI Filter Function for selecting Responses API reasoning effort. The adapter remains responsible for translating Open WebUI's flat `reasoning_effort` field to the Responses API `reasoning.effort` object.

## Install

1. In Open WebUI, open **Admin Panel → Functions** and create a Filter Function.
2. Copy the source from [`functions/siwc_think.py`](functions/siwc_think.py), save it, and enable it for the SIWC models.
3. Keep its **Think** toggle available in the composer. Configure the per-user `reasoning_effort` setting in the Function's user settings. The default is `high` when Think is enabled.
4. Turn Think off to use Auto/default behavior. The Filter is skipped while off, so an explicit Advanced Parameters value is left untouched.

Open WebUI's native **Chat Controls → Advanced Parameters → Reasoning Effort** remains available as a fallback. The Filter does not modify prompts, model IDs, credentials, unrelated request fields, filesystem state, or external state. It makes no network requests.

## Supported model values

OpenAI's current model catalog returns model IDs but does not include per-model reasoning-effort capability metadata. The Filter therefore uses an exact model-ID capability map validated against the current Responses API model documentation. An unknown model is left unchanged. If the selected effort is unsupported for a known model, the Filter raises an error rather than silently changing it.

| Current model IDs | Supported efforts |
| --- | --- |
| [gpt-5.6-luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna), [gpt-5.6-terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra), [gpt-5.6-sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol), [gpt-6-sol](https://developers.openai.com/api/docs/models/gpt-6-sol), [gpt-6-luna](https://developers.openai.com/api/docs/models/gpt-6-luna) | `none`, `low`, `medium`, `high`, `xhigh`, `max` |
| [gpt-6-astra](https://developers.openai.com/api/docs/models/gpt-6-astra), [gpt-6.1-sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol) | `low`, `medium`, `high`, `xhigh`, `max` |

These values were checked against each linked OpenAI model page on 2026-10-09. In particular, `none` is not supported by GPT-6 Astra or GPT-6.1 Sol. The selector presents the union of current values; the Filter enforces the selected model's exact set. Open WebUI v0.11.4 does not provide a model-reactive user-setting schema, so unsupported choices are rejected when used.

Review the provider's current model documentation and update the exact-ID map when its catalog or capability contract changes. Do not infer new model capabilities from a model name or silently map one effort to another.

## Verification

`tests/test_open_webui_reasoning_filter.py` checks exact forwarding for every declared model/effort pair, rejection of unsupported values, and inert behavior for unknown models. Live production transport acceptance for the currently installed Function is recorded in the private operations handoff; CI does not perform SIWC authorization or make live model requests.
