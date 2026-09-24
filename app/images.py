import base64
from dataclasses import dataclass, field
from io import BytesIO
import re

from PIL import Image, ImageOps


MAX_IMAGE_BYTES = 20 * 1024 * 1024

IMAGE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'generate_image',
        'description': ('Generate and send one image to this Instagram group. Use only when the latest '
                        'user asks to create/draw an image. Resolve references using the conversation '
                        'and supply a complete image prompt. When the request is based on a group '
                        "member's avatar, set reference_username to that exact group username."),
        'parameters': {
            'type': 'object',
            'properties': {
                'prompt': {'type': 'string', 'description': 'Complete description of the image to create.'},
                'reference_username': {
                    'type': 'string',
                    'description': 'Exact Instagram username whose group profile picture should be used as reference.',
                },
            },
            'required': ['prompt'],
            'additionalProperties': False,
        },
    },
}


@dataclass
class GeneratedImage:
    prompt: str
    jpeg: bytes = field(repr=False)


def normalize_reference_image(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError('Reference image exceeds 20 MiB or is empty')
    with Image.open(BytesIO(raw)) as source:
        if source.width * source.height > 25_000_000:
            raise ValueError('Reference image exceeds 25 megapixels')
        normalized = ImageOps.exif_transpose(source)
        normalized.thumbnail((1024, 1024))
        rgba = normalized.convert('RGBA')
        background = Image.new('RGB', rgba.size, 'white')
        background.paste(rgba, mask=rgba.getchannel('A'))
        output = BytesIO()
        background.save(output, format='JPEG', quality=90)
        return output.getvalue()


def decode_image(message):
    """Read inline image outputs; never fetch model-supplied URLs."""
    parts = list(message.get('images') or [])
    content = message.get('content')
    if isinstance(content, list):
        parts.extend(content)
    elif isinstance(content, str):
        # Some compatible gateways wrap the inline image in Markdown.
        parts.extend({'image_url': {'url': match}} for match in re.findall(
            r'data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+', content))
    for part in parts:
        if not isinstance(part, dict):
            continue
        image_url = part.get('image_url')
        url = image_url.get('url') if isinstance(image_url, dict) else image_url
        if not isinstance(url, str) or not url.startswith('data:image/'):
            continue
        header, separator, encoded = url.partition(',')
        if not separator or not header.endswith(';base64'):
            continue
        if len(encoded) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
            raise ValueError('Generated image exceeds 20 MiB')
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError('Generated image exceeds 20 MiB')
        with Image.open(BytesIO(raw)) as source:
            if source.width * source.height > 25_000_000:
                raise ValueError('Generated image exceeds 25 megapixels')
            normalized = ImageOps.exif_transpose(source)
            normalized.thumbnail((1080, 1080))
            rgba = normalized.convert('RGBA')
            background = Image.new('RGB', rgba.size, 'white')
            background.paste(rgba, mask=rgba.getchannel('A'))
            output = BytesIO()
            background.save(output, format='JPEG', quality=90)
            return output.getvalue()
    raise ValueError('Image model returned no inline image')
