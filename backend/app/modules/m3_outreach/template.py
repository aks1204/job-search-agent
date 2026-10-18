"""The owner's email template (config/email_template.md): settings, the email as it goes out, and the
instructions for the parts the AI writes. One swappable file (spec §3.2); edit + restart to change."""

import hashlib
import re
from dataclasses import dataclass

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config_loader import get_config
from app.settings import get_settings

AI_PARTS = ("intro", "points")
INLINE_OPTIONAL = {"req_ids"}  # may be empty without dropping the line it sits on


class TemplateError(RuntimeError):
    pass


class TemplateSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str
    subject_max_chars: int = Field(90, ge=30, le=200)
    greeting: str = "Hi {first_name},"
    greeting_without_name: str = "Hi there,"
    points_min: int = Field(3, ge=0, le=10)
    points_max: int = Field(6, ge=1, le=12)
    attach_resume: bool = True


@dataclass(frozen=True)
class Template:
    settings: TemplateSettings
    body: str                     # the email as it goes out, with {placeholders} and {{intro}}/{{points}}
    sections: dict[str, str]      # "## name" instruction sections: subject, intro, points, rules…
    version: str


def parse_template(text: str) -> Template:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if not m:
        raise TemplateError("email template must start with a settings block between --- lines")
    try:
        settings = TemplateSettings.model_validate(yaml.safe_load(m.group(1)) or {})
    except (yaml.YAMLError, ValidationError) as e:
        raise TemplateError(f"email template settings: {e}") from e
    rest = m.group(2)
    parts = re.split(r"^## +(\w+)\s*$", rest, flags=re.M)
    body, sections = parts[0].strip("\n"), {}
    for name, content in zip(parts[1::2], parts[2::2]):
        sections[name.lower()] = content.strip()
    for part in AI_PARTS:
        if "{{" + part + "}}" not in body:
            raise TemplateError(f"email template body needs a {{{{{part}}}}} line")
    if "{greeting}" not in body:
        raise TemplateError("email template body needs a {greeting} line")
    version = hashlib.sha256(text.encode()).hexdigest()[:16]
    return Template(settings, body, sections, version)


def load_template() -> Template:
    path = get_settings().config_dir / get_config().outreach.drafts.template_file
    if not path.is_file():
        raise TemplateError(f"email template not found: config/{path.name}")
    return parse_template(path.read_text(encoding="utf-8"))


_PLACEHOLDER = re.compile(r"(?<!\{)\{(\w+)\}(?!\})")


def fill(text: str, values: dict[str, str | None], *, drop_lines: bool = True) -> str:
    """Fill {placeholders} we know; a line whose (non-inline) placeholder has no value is left out."""
    out = []
    for line in text.split("\n"):
        names = [n for n in _PLACEHOLDER.findall(line) if n in values]
        if drop_lines and any(not values[n] and n not in INLINE_OPTIONAL for n in names):
            continue
        out.append(_PLACEHOLDER.sub(lambda mm: str(values.get(mm.group(1)) or "") if mm.group(1) in values
                                    else mm.group(0), line))
    return "\n".join(out)


def fit_subject(t: Template, values: dict[str, str | None], role_short: str) -> str:
    """Fill the subject; if it's too long, drop words from the end of the role until it fits."""
    words = role_short.split()
    while True:
        subject = fill(t.settings.subject, {**values, "role_short": " ".join(words)}, drop_lines=False)
        subject = " ".join(re.sub(r"\(\s*(YoE)?\s*\)", "", subject).split())  # e.g. no years on file
        if len(subject) <= t.settings.subject_max_chars or len(words) <= 1:
            return subject
        words = words[:-1]
