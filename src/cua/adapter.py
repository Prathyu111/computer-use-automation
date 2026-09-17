"""Web surface adapter: a11y-first observe/act. Playwright stays inside this module."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeout

from cua.models import LocatorStrategy, ProposedAction, Target


@dataclass
class ObserveResult:
    url: str
    title: str
    heading: str
    body_text: str
    controls: list[dict[str, str]]
    dialog: str | None


class SurfaceAdapter:
    def __init__(self, page: Page) -> None:
        self.page = page

    def observe(self) -> ObserveResult:
        heading = ""
        try:
            heading = self.page.locator("h1, h2").first.inner_text(timeout=1000)
        except Exception:
            heading = ""
        body = ""
        try:
            body = self.page.locator("body").inner_text(timeout=2000)
        except Exception:
            body = ""
        controls: list[dict[str, str]] = []
        for role, selector in (
            ("textbox", "input:not([type='hidden']), textarea"),
            ("button", "button, input[type='submit']"),
        ):
            loc = self.page.locator(selector)
            count = min(loc.count(), 20)
            for i in range(count):
                el = loc.nth(i)
                name = (el.get_attribute("aria-label") or "").strip()
                if not name:
                    try:
                        name = el.inner_text(timeout=500).strip()
                    except Exception:
                        name = el.get_attribute("name") or el.get_attribute("placeholder") or ""
                controls.append({"role": role, "name": name[:80]})
        dialog = None
        overlay = self.page.locator(".overlay, [role='alertdialog'], dialog")
        try:
            if overlay.count() and overlay.first.is_visible():
                dialog = overlay.first.inner_text(timeout=1000)[:400]
        except Exception:
            dialog = None
        return ObserveResult(
            url=self.page.url,
            title=self.page.title(),
            heading=heading.strip(),
            body_text=body[:4000],
            controls=controls,
            dialog=dialog,
        )

    def observation_for_llm(self, obs: ObserveResult) -> dict[str, Any]:
        return {
            "url": obs.url,
            "title": obs.title,
            "heading": obs.heading,
            "dialog": obs.dialog,
            "controls": obs.controls,
            "visible_text": obs.body_text[:2500],
        }

    def act_proposed(self, action: ProposedAction) -> None:
        if action.type == "goto" and action.url:
            self.page.goto(action.url, wait_until="domcontentloaded")
            return
        if action.type == "dismiss":
            self._dismiss()
            return
        if action.type in {"click", "type", "extract"}:
            target = Target(
                intent=action.intent or action.name or action.text or action.type,
                strategies=self._strategies_from_proposed(action),
            )
            loc = self.resolve(target)
            if action.type == "click":
                loc.click()
            elif action.type == "type":
                loc.fill(action.value or "")
            elif action.type == "extract":
                loc.inner_text()
            return
        raise ValueError(f"unsupported proposed action {action.type}")

    def act_step(
        self,
        action: str,
        target: Target | None,
        *,
        value: str | None = None,
        url: str | None = None,
        key: str | None = None,
        timeout_ms: int = 8000,
    ) -> str | None:
        if action == "goto" and url:
            self.page.goto(url, wait_until="domcontentloaded")
            return None
        if action == "dismiss":
            self._dismiss()
            return None
        if action == "wait_until":
            self.page.wait_for_timeout(min(timeout_ms, 2000))
            return None
        if action == "press_key" and key:
            self.page.keyboard.press(key)
            return None
        if target is None:
            raise ValueError(f"step {action} requires a target")
        loc = self.resolve(target, timeout_ms=timeout_ms)
        if action == "click":
            loc.click()
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            return None
        if action in {"type", "clear_and_type"}:
            loc.fill(value or "")
            return None
        if action == "extract":
            return loc.inner_text().strip()
        raise ValueError(f"unsupported step action {action}")

    def resolve(self, target: Target, timeout_ms: int = 8000) -> Locator:
        errors: list[str] = []
        for strategy in target.strategies:
            try:
                loc = self._locator_for(strategy)
                loc.wait_for(state="visible", timeout=timeout_ms)
                count = loc.count()
                if target.match == "one" and count != 1:
                    errors.append(f"{strategy.kind}: expected 1 match, got {count}")
                    continue
                return loc.first
            except PlaywrightTimeout:
                errors.append(f"{strategy.kind}: timeout")
            except Exception as exc:
                errors.append(f"{strategy.kind}: {exc}")
        raise LookupError(
            f"could not uniquely resolve '{target.intent}': " + "; ".join(errors)
        )

    def checkpoint_ok(self, heading_contains: str | None, text_contains: str | None, url_contains: str | None) -> bool:
        obs = self.observe()
        if heading_contains and heading_contains.lower() not in obs.heading.lower():
            return False
        if text_contains and text_contains.lower() not in obs.body_text.lower():
            return False
        if url_contains and url_contains.lower() not in obs.url.lower():
            return False
        return True

    def text_matches(self, text: str, scope: str | None = None) -> bool:
        hay = self.observe().body_text
        if scope:
            hay = scope + "\n" + hay
        return text.lower() in hay.lower()

    def _dismiss(self) -> None:
        for name in ("OK", "Dismiss", "Close", "Got it"):
            btn = self.page.get_by_role("button", name=re.compile(name, re.I))
            try:
                if btn.count() and btn.first.is_visible():
                    btn.first.click()
                    return
            except Exception:
                continue
        overlay_btn = self.page.locator(".overlay button")
        if overlay_btn.count():
            overlay_btn.first.click()

    def _strategies_from_proposed(self, action: ProposedAction) -> list[LocatorStrategy]:
        strategies: list[LocatorStrategy] = []
        if action.role or action.name:
            strategies.append(
                LocatorStrategy(kind="a11y", role=action.role, name=action.name)
            )
        if action.name or action.text or action.intent:
            label = action.name or action.text or action.intent
            strategies.append(LocatorStrategy(kind="labeled_control", label=label))
            strategies.append(LocatorStrategy(kind="name_in_scope", text=label))
        if not strategies:
            strategies.append(LocatorStrategy(kind="a11y", role="button", name=action.intent))
        return strategies

    def _locator_for(self, strategy: LocatorStrategy) -> Locator:
        if strategy.kind == "a11y":
            role = strategy.role or "button"
            kwargs: dict[str, Any] = {}
            if strategy.name:
                kwargs["name"] = re.compile(strategy.name, re.I)
            return self.page.get_by_role(role, **kwargs)
        if strategy.kind == "labeled_control":
            label = strategy.label or strategy.name or ""
            row = self.page.locator("tr").filter(has_text=re.compile(rf"^{re.escape(label)}$|{re.escape(label)}", re.I))
            labeled = row.locator("input, textarea, select, button")
            if labeled.count():
                return labeled
            return self.page.get_by_label(re.compile(label, re.I))
        if strategy.kind == "name_in_scope":
            text = strategy.text or strategy.name or ""
            return self.page.get_by_text(re.compile(text, re.I))
        if strategy.kind == "structural":
            key = strategy.row_key or ""
            row = self.page.locator("tr").filter(has_text=re.compile(key, re.I))
            cell = strategy.target_cell if strategy.target_cell is not None else 1
            return row.locator("td").nth(cell)
        if strategy.kind == "css" and strategy.css:
            return self.page.locator(strategy.css)
        raise ValueError(f"unknown strategy {strategy.kind}")
