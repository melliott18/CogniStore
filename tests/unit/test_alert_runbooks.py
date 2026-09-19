"""Keep deployed alert annotations attached to actionable, shipped procedures."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
RULE_PATH = ROOT / "docker/observability/rules/slo.yml"
ALERTS = [
    rule
    for group in yaml.safe_load(RULE_PATH.read_text(encoding="utf-8"))["groups"]
    for rule in group["rules"]
    if "alert" in rule
]


@pytest.mark.parametrize("rule", ALERTS, ids=lambda rule: rule["alert"])
def test_every_deployed_alert_links_to_an_actionable_runbook(rule: dict) -> None:
    url = urlsplit(rule["annotations"]["runbook_url"])
    assert url.scheme == "https"
    assert url.netloc == "github.com"
    prefix = "/melliott18/CogniStore/blob/main/"
    assert url.path.startswith(prefix)
    path = ROOT / url.path.removeprefix(prefix)
    assert path.resolve().is_relative_to(ROOT / "docs")
    assert path.is_file(), f"Missing runbook for {rule['alert']}: {path}"
    assert url.fragment, f"Alert should link to its response section: {rule['alert']}"

    document = path.read_text(encoding="utf-8")
    # Runbook response sections use plain level-two headings. End the section at
    # the next peer heading so a later procedure cannot accidentally satisfy it.
    sections = re.split(r"^## (.+)$", document, flags=re.MULTILINE)
    matches = [
        body
        for title, body in zip(sections[1::2], sections[2::2])
        if re.sub(r"[^\w -]", "", title).lower().replace(" ", "-") == url.fragment
    ]
    assert len(matches) == 1, f"Missing/ambiguous runbook anchor: {url.fragment}"
    response = matches[0]
    assert rule["alert"] in response, "The linked procedure must identify the alert it handles"
    for phase in ("Triage", "Action", "Verify", "Escalate"):
        assert f"**{phase}.**" in response, f"Runbook lacks {phase}: {rule['alert']}"


def test_deployed_alert_contract_is_not_empty() -> None:
    assert ALERTS, "The runbook coverage test must examine the deployed alert rules"
