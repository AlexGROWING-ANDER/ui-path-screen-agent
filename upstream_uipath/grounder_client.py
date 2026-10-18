import httpx
import mm_agents.uipath.utils as utils
import os
import base64
import io
import json
import re

from PIL import Image

class GrounderClient(object):
    def __init__(self):
        # Proxy for hosting finetuned Qwen3VL + UiElementPredictor
        # Could be replaced with a VLLM server and grounder specific processing
        self.url = os.getenv("UIPATH_INTERNAL_GROUNDER_URL", "").strip()

    async def _predict_openai_compatible(
        self, image_base64: str, action_description: str, element_description: str | None
    ) -> utils.GroundingOutput:
        """Adapt a 0-1000 visual grounder response to pixel coordinates."""
        from openai import AsyncOpenAI

        api_key = os.getenv("UIPATH_GROUND_API_KEY", "").strip()
        model = os.getenv("UIPATH_GROUND_MODEL", "").strip()
        if not api_key or not model:
            raise RuntimeError("UIPATH_GROUND_API_KEY and UIPATH_GROUND_MODEL are required")

        target = element_description or action_description
        prompt = (
            f"Query: {target}\n"
            "Locate the target in this screenshot. Reply with exactly one "
            "coordinate pair in the form (x, y), normalized to 0-1000. "
            "Do not include any explanation."
        )
        with Image.open(io.BytesIO(base64.b64decode(image_base64))) as screenshot:
            width, height = screenshot.size
            mime_type = "image/jpeg" if screenshot.format == "JPEG" else "image/png"
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=os.environ["UIPATH_GROUND_BASE_URL"],
            timeout=180.0,
        )
        response = await client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_base64}"}},
                ],
            }],
        )
        content = response.choices[0].message.content or ""
        pair = re.search(r"[\[(]\s*(\d+(?:\.\d+)?)\s*[,，]\s*(\d+(?:\.\d+)?)\s*[\])]", content)
        if pair:
            normalized_x, normalized_y = float(pair.group(1)), float(pair.group(2))
        else:
            try:
                parsed = json.loads(content)
                normalized_x, normalized_y = float(parsed["x"]), float(parsed["y"])
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                numbers = re.findall(r"\d+(?:\.\d+)?", content)
                if len(numbers) != 2:
                    raise utils.GroundingOutputValidationException(
                        "Grounder did not return one unambiguous coordinate pair", target, content
                    )
                normalized_x, normalized_y = float(numbers[0]), float(numbers[1])
        if not (0 <= normalized_x <= 1000 and 0 <= normalized_y <= 1000):
            raise utils.GroundingOutputValidationException(
                "Grounder coordinate outside 0-1000 space", target, content
            )
        position = (
            round(normalized_x * width / 1000),
            round(normalized_y * height / 1000),
        )
        return utils.GroundingOutput(description=target, position=position)

    async def predict(
        self, image_base64: str, action_description: str, action: str, element_description: str | None = None,
    ) -> utils.GroundingOutput:
        if os.getenv("UIPATH_GROUND_BASE_URL", "").strip():
            return await self._predict_openai_compatible(
                image_base64, action_description, element_description
            )
        if not self.url:
            raise RuntimeError("Set UIPATH_GROUND_BASE_URL or UIPATH_INTERNAL_GROUNDER_URL")
        request = utils.GroundingRequest(
            description=action_description,
            image_base64=image_base64,
            action_type=action,
            element_description=element_description
        )
        api_key = os.getenv("SERVICE_KEY")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.url,
                json={
                    "image_base64": request.image_base64,
                    "action_description": request.description,
                    "action": request.action_type,
                    "element_description": request.element_description,
                },
                headers={
                    "X-API-KEY": api_key
                },
                timeout=100.0,
            )

        if response.status_code != 200:
            raise ValueError(f"Prediction failed: {response.text}")

        data = response.json()
        if tuple(data["position"]) == (-1, -1):
            raise utils.GroundingOutputValidationException(f"Element {request.description} not found in image", request.description)
        return utils.GroundingOutput(
            description=data["description"],
            position=tuple(data["position"]),
        )
