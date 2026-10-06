"""The default template for both frozen report inputs.

Changing layout/assets/copy requires a template-version bump; changing the
rendering algorithm requires a renderer-version bump. Neither changes inputs.
"""
import reportlab
import re

TEMPLATE_ID = "iaa-csc"
TEMPLATE_VERSION = "9"
RENDERER_VERSION = "reportlab-2"


def rendering_metadata() -> dict[str, str]:
    return {"template_id": TEMPLATE_ID, "template_version": TEMPLATE_VERSION,
            "renderer_version": RENDERER_VERSION, "reportlab_version": reportlab.Version}


def render_identity() -> str:
    meta = rendering_metadata()
    return f"{meta['template_id']}-v{meta['template_version']}-{meta['renderer_version']}-rl{meta['reportlab_version']}"


def is_render_identity(value: str) -> bool:
    """Recognize retained render paths, including earlier default versions."""
    return re.fullmatch(r"iaa-csc-v[1-9][0-9]*-reportlab-[1-9][0-9]*-rl[0-9][0-9A-Za-z.]*", value) is not None
