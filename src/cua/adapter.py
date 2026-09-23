"""Web surface adapter: a11y-first observe/act. Playwright stays inside this module."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import Frame, Locator, Page, TimeoutError as PlaywrightTimeout

from cua.models import FrameScope, LocatorStrategy, ProposedAction, Target
from cua.redact import redact_for_log, redact_text


@dataclass
class ObserveResult:
    url: str
    title: str
    heading: str
    body_text: str
    controls: list[dict[str, Any]]
    dialog: str | None
    labeled_fields: list[dict[str, Any]] = field(default_factory=list)


class SurfaceMismatchError(LookupError):
    """Required overlay surface/frame contract unsatisfied. Fail closed.

    Replay maps this to kind=hard_failure, code=surface_mismatch.
    It is not business_outcome and is not a vendor-version claim.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = "surface_mismatch"


class SurfaceAdapter:
    def __init__(
        self,
        page: Page,
        frame_scope: FrameScope | None = None,
        surface_contract: bool = False,
    ) -> None:
        self.page = page
        self.frame_scope = frame_scope
        self.surface_contract = surface_contract

    def observe(self) -> ObserveResult:
        root = self._root()
        heading = ""
        try:
            heading = root.locator("h1, h2").first.inner_text(timeout=1000)
        except Exception:
            heading = ""
        body = ""
        try:
            body = root.locator("body").inner_text(timeout=2000)
        except Exception:
            body = ""
        controls: list[dict[str, Any]] = []
        for role, selector in (
            ("textbox", "input:not([type='hidden']), textarea"),
            ("button", "button, input[type='submit']"),
        ):
            loc = root.locator(selector)
            count = min(loc.count(), 20)
            for i in range(count):
                el = loc.nth(i)
                name = (el.get_attribute("aria-label") or "").strip()
                if not name:
                    try:
                        name = el.inner_text(timeout=500).strip()
                    except Exception:
                        name = el.get_attribute("name") or el.get_attribute("placeholder") or ""
                control: dict[str, Any] = {"role": role, "name": name[:80]}
                if role == "textbox":
                    control["filled"] = self._control_is_filled(el)
                controls.append(control)
        dialog = None
        overlay = root.locator(".overlay, [role='alertdialog'], dialog")
        try:
            if overlay.count() and overlay.first.is_visible():
                dialog = overlay.first.inner_text(timeout=1000)[:400]
        except Exception:
            dialog = None
        return ObserveResult(
            url=root.url,
            title=root.title(),
            heading=heading.strip(),
            body_text=body[:4000],
            controls=controls,
            dialog=dialog,
            labeled_fields=self._labeled_readonly_pairs(),
        )

    def observation_for_llm(self, obs: ObserveResult) -> dict[str, Any]:
        return {
            "url": obs.url,
            "title": obs.title,
            "heading": obs.heading,
            "dialog": redact_text(obs.dialog or "") or None,
            "controls": obs.controls,
            "labeled_fields": redact_for_log("labeled_fields", obs.labeled_fields),
            "visible_text": redact_text(obs.body_text[:800]),
        }

    def act_proposed(self, action: ProposedAction) -> str | None:
        if action.type == "goto" and action.url:
            self.page.goto(action.url, wait_until="domcontentloaded")
            return None
        if action.type == "dismiss":
            self._dismiss()
            return None
        if action.type in {"click", "type", "extract"}:
            if action.type in {"click", "type"}:
                self.try_dismiss()
            strategies = self._strategies_from_proposed(action)
            if not strategies:
                raise LookupError(
                    "could not uniquely resolve extract: "
                    "extract requires name or text as the field label"
                )
            target = Target(
                intent=action.intent or action.name or action.text or action.type,
                strategies=strategies,
            )
            loc = self.resolve(target, need=_need_for_action(action.type))
            if action.type == "type":
                loc.fill(action.value or "", timeout=8000)
                return None
            if action.type == "click":
                loc.click()
                self._wait_dom()
                if self.try_dismiss() == "dismissed":
                    loc.click()
                    self._wait_dom()
                return None
            return loc.inner_text().strip()
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
            self.try_dismiss()
            return None
        if action == "press_key" and key:
            self.page.keyboard.press(key)
            return None
        if target is None:
            raise ValueError(f"step {action} requires a target")
        loc = self.resolve(
            target, timeout_ms=timeout_ms, need=_need_for_action(action)
        )
        if action == "click":
            loc.click()
            self._wait_dom()
            return None
        if action in {"type", "clear_and_type"}:
            loc.fill(value or "")
            return None
        if action == "extract":
            return loc.inner_text().strip()
        raise ValueError(f"unsupported step action {action}")

    def resolve(
        self,
        target: Target,
        timeout_ms: int = 8000,
        *,
        need: str | None = None,
    ) -> Locator:
        errors: list[str] = []
        for strategy in target.strategies:
            tag = f"{strategy.kind}:{_strategy_locator_name(strategy)}"
            try:
                loc = self._locator_for(strategy)
                count = loc.count()
                if target.match == "one" and count != 1:
                    errors.append(f"{tag}: expected 1 match, got {count}")
                    continue
                if count == 0:
                    errors.append(f"{tag}: expected 1 match, got 0")
                    continue
                chosen = loc.first
                chosen.wait_for(state="visible", timeout=timeout_ms)
                if need == "editable" and not self._is_editable(chosen):
                    errors.append(f"{tag}: unique match is not editable")
                    continue
                if need == "clickable" and not self._is_clickable(chosen):
                    errors.append(f"{tag}: unique match is not clickable")
                    continue
                return chosen
            except SurfaceMismatchError:
                raise
            except PlaywrightTimeout:
                errors.append(f"{tag}: timeout")
            except Exception as exc:
                errors.append(f"{tag}: {exc}")
        tried = _unique_strategy_locator_names(target.strategies)
        message = f"could not uniquely resolve '{tried}': " + "; ".join(errors)
        err = LookupError(message)
        if self.surface_contract:
            raise SurfaceMismatchError(message) from err
        raise err

    def checkpoint_ok(self, heading_contains: str | None, text_contains: str | None, url_contains: str | None) -> bool:
        obs = self.observe()
        if heading_contains and heading_contains.lower() not in obs.heading.lower():
            return self._checkpoint_result(False, heading_contains, obs.heading)
        if text_contains and text_contains.lower() not in obs.body_text.lower():
            return self._checkpoint_result(False, text_contains, obs.body_text[:200])
        if url_contains and url_contains.lower() not in obs.url.lower():
            return self._checkpoint_result(False, url_contains, obs.url)
        return True

    def _checkpoint_result(self, ok: bool, expected: str | None, observed: str) -> bool:
        if ok:
            return True
        if self.surface_contract:
            raise SurfaceMismatchError(
                f"required surface checkpoint unsatisfied: expected {expected!r}, observed {observed!r}"
            )
        return False

    def text_matches(self, text: str) -> bool:
        return text.lower() in self.observe().body_text.lower()

    def try_dismiss(self) -> str:
        """Return 'dismissed' or 'absent'. Raise on automation failure."""
        try:
            root = self._root()
            for name in ("OK", "Dismiss", "Close", "Got it"):
                btn = root.get_by_role("button", name=re.compile(name, re.I))
                if btn.count() and btn.first.is_visible():
                    btn.first.click()
                    return "dismissed"
            overlay_btn = root.locator(".overlay button")
            if overlay_btn.count() and overlay_btn.first.is_visible():
                overlay_btn.first.click()
                return "dismissed"
            return "absent"
        except PlaywrightTimeout:
            return "absent"

    def _dismiss(self) -> None:
        self.try_dismiss()

    def _strategies_from_proposed(self, action: ProposedAction) -> list[LocatorStrategy]:
        if action.type == "extract":
            label = (action.name or action.text or "").strip()
            if not label:
                return []
            return [
                LocatorStrategy(kind="labeled_readonly", label=label),
                LocatorStrategy(kind="structural", row_key=label, target_cell=1),
            ]
        strategies: list[LocatorStrategy] = []
        label = action.name or action.text or action.intent
        if action.role or action.name:
            strategies.append(
                LocatorStrategy(
                    kind="a11y",
                    role=action.role or ("textbox" if action.type == "type" else "button"),
                    name=action.name,
                )
            )
        if label:
            strategies.append(LocatorStrategy(kind="labeled_control", label=label))
            strategies.append(LocatorStrategy(kind="name_in_scope", text=label))
        if not strategies:
            strategies.append(
                LocatorStrategy(
                    kind="a11y",
                    role="textbox" if action.type == "type" else "button",
                    name=action.intent,
                )
            )
        return strategies

    def _control_is_filled(self, loc: Locator) -> bool:
        """Whether an editable control has content, without returning the value."""
        try:
            value = loc.input_value(timeout=500)
            return bool((value or "").strip())
        except Exception:
            try:
                value = loc.evaluate("el => (el.value ?? '')")
                return bool(str(value or "").strip())
            except Exception:
                return False

    def _is_editable(self, loc: Locator) -> bool:
        info = loc.evaluate(
            """el => {
              const tag = el.tagName.toLowerCase();
              if (el.isContentEditable) return true;
              if (tag === 'textarea' || tag === 'select') return true;
              if (tag === 'input') {
                const t = (el.getAttribute('type') || 'text').toLowerCase();
                return !['button','submit','reset','checkbox','radio','file','hidden','image'].includes(t);
              }
              return false;
            }"""
        )
        return bool(info)

    def _is_clickable(self, loc: Locator) -> bool:
        info = loc.evaluate(
            """el => {
              const tag = el.tagName.toLowerCase();
              const role = (el.getAttribute('role') || '').toLowerCase();
              if (['a','button','summary','option'].includes(tag)) return true;
              if (tag === 'input') return true;
              if (['button','link','tab','menuitem','checkbox','radio','switch'].includes(role)) return true;
              return false;
            }"""
        )
        return bool(info)

    def _locator_for(self, strategy: LocatorStrategy) -> Locator:
        root = self._root()
        if strategy.kind == "a11y":
            role = strategy.role or "button"
            kwargs: dict[str, Any] = {}
            if strategy.name:
                kwargs["name"] = strategy.name
            return root.get_by_role(role, **kwargs)
        if strategy.kind == "labeled_control":
            return self._labeled_editable(strategy.label or strategy.name or "")
        if strategy.kind == "labeled_readonly":
            return self._labeled_readonly(strategy.label or strategy.name or strategy.text or "")
        if strategy.kind == "name_in_scope":
            text = strategy.text or strategy.name or ""
            return root.get_by_text(re.compile(text, re.I))
        if strategy.kind == "structural":
            key = strategy.row_key or ""
            row = root.locator("tr").filter(has_text=re.compile(key, re.I))
            cell = strategy.target_cell if strategy.target_cell is not None else 1
            return row.locator("td").nth(cell)
        if strategy.kind == "css" and strategy.css:
            return root.locator(strategy.css)
        raise ValueError(f"unknown strategy {strategy.kind}")

    def _labeled_editable(self, label: str) -> Locator:
        """Associate visible label/table-header text with a unique nearby editable control."""
        root = self._root()
        exact = re.compile(rf"^{re.escape(label)}$", re.I)
        by_label = root.get_by_label(label, exact=False)
        if by_label.count() == 1:
            return by_label
        cells = root.locator("th, td, label").filter(has_text=exact)
        if cells.count() == 1:
            row_editables = cells.locator("xpath=ancestor::tr[1]").locator(
                "input:not([type='hidden']):not([type='submit']):not([type='button']), "
                "textarea, select, [contenteditable='true']"
            )
            if row_editables.count() == 1:
                return row_editables
            following = cells.locator("xpath=following-sibling::*[1]").locator(
                "input:not([type='hidden']), textarea, select, [contenteditable='true']"
            )
            if following.count() == 1:
                return following
        return root.locator("[data-cua-unresolved-labeled-control]")

    def _labeled_readonly(self, label: str) -> Locator:
        """Unique labeled read-only field, including table-style label → adjacent value."""
        root = self._root()
        unresolved = root.locator("[data-cua-unresolved-labeled-readonly]")
        if not label:
            return unresolved
        exact = re.compile(rf"^{re.escape(label)}$", re.I)
        cells = self.page.locator("th, td, dt, label").filter(has_text=exact)
        if cells.count() != 1:
            return unresolved
        following = cells.locator("xpath=following-sibling::*[1]")
        if following.count() != 1:
            return unresolved
        if following.locator(
            "input, textarea, select, [contenteditable='true']"
        ).count():
            return unresolved
        return following

    def _labeled_readonly_pairs(self) -> list[dict[str, Any]]:
        try:
            pairs = self._root().evaluate(
                """() => {
                  const seen = new Map();
                  const dup = new Set();
                  const add = (label, value) => {
                    label = String(label || '').trim().replace(/\\s+/g, ' ');
                    value = String(value || '').trim().replace(/\\s+/g, ' ');
                    if (!label || !value) return;
                    if (seen.has(label) && seen.get(label) !== value) dup.add(label);
                    else if (!seen.has(label)) seen.set(label, value);
                  };
                  const isEditable = (el) =>
                    !!(el && el.querySelector(
                      "input, textarea, select, [contenteditable='true']"
                    ));
                  for (const tr of document.querySelectorAll('tr')) {
                    const cells = [...tr.querySelectorAll(':scope > th, :scope > td')];
                    if (cells.length < 2 || isEditable(cells[1])) continue;
                    add(cells[0].innerText, cells[1].innerText);
                  }
                  for (const dt of document.querySelectorAll('dt')) {
                    const dd = dt.nextElementSibling;
                    if (dd && dd.tagName === 'DD' && !isEditable(dd)) {
                      add(dt.innerText, dd.innerText);
                    }
                  }
                  const out = [];
                  for (const [label, value] of seen) {
                    if (!dup.has(label)) out.push({label, value});
                  }
                  return out.slice(0, 30);
                }"""
            )
        except Exception:
            return []
        if not isinstance(pairs, list):
            return []
        cleaned: list[dict[str, Any]] = []
        for item in pairs:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            value = str(item.get("value") or "").strip()
            if label and value:
                cleaned.append({"label": label[:80], "value": value})
        return cleaned

    def _wait_dom(self) -> None:
        try:
            self._root().wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass

    def _root(self) -> Page | Frame:
        """Scoped document root. No silent fallback to the parent page."""
        if self.frame_scope is None:
            return self.page
        selector = self.frame_scope.selector
        loc = self.page.locator(selector)
        try:
            loc.first.wait_for(state="attached", timeout=5000)
            count = loc.count()
        except PlaywrightTimeout as exc:
            raise SurfaceMismatchError(
                f"required iframe not bound ({selector!r}): not attached"
            ) from exc
        except Exception as exc:
            raise SurfaceMismatchError(
                f"required iframe not bound ({selector!r}): {exc}"
            ) from exc
        if count != 1:
            raise SurfaceMismatchError(
                f"required iframe not uniquely bound ({selector!r}): expected 1, got {count}"
            )
        handle = loc.first.element_handle()
        if handle is None:
            raise SurfaceMismatchError(
                f"required iframe not bound ({selector!r}): no element handle"
            )
        frame = handle.content_frame()
        if frame is None:
            raise SurfaceMismatchError(
                f"required iframe content unavailable ({selector!r})"
            )
        return frame


def _need_for_action(action: str) -> str | None:
    if action in {"type", "clear_and_type"}:
        return "editable"
    if action == "click":
        return "clickable"
    return None


def _strategy_locator_name(strategy: LocatorStrategy) -> str:
    for candidate in (
        strategy.label,
        strategy.name,
        strategy.row_key,
        strategy.text,
        strategy.css,
        strategy.role,
    ):
        if candidate:
            return candidate
    return strategy.kind


def _unique_strategy_locator_names(strategies: list[LocatorStrategy]) -> str:
    names: list[str] = []
    seen: set[str] = set()
    for strategy in strategies:
        name = _strategy_locator_name(strategy)
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return ", ".join(names) if names else "locator"
