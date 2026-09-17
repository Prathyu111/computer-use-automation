"""Live session + exclusive control lock. One browser context per run."""

from __future__ import annotations

import os

from playwright.sync_api import Browser, Page, Playwright, sync_playwright

from cua.models import ControlLock


class Session:
    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self.lock = ControlLock.agent
        self._pw: Playwright | None = None
        self.browser: Browser | None = None
        self.page: Page | None = None

    def start(self, url: str) -> Page:
        self._pw = sync_playwright().start()
        channel = os.environ.get("CUA_BROWSER_CHANNEL", "chrome")
        launch_kwargs: dict = {"headless": self.headless}
        # Prefer installed Chrome so we do not depend on Playwright's CDN download.
        if channel:
            launch_kwargs["channel"] = channel
        try:
            self.browser = self._pw.chromium.launch(**launch_kwargs)
        except Exception:
            self.browser = self._pw.chromium.launch(headless=self.headless)
        self.page = self.browser.new_page()
        self.lock = ControlLock.agent
        self.page.goto(url, wait_until="domcontentloaded")
        return self.page

    def current_url(self) -> str:
        assert self.page is not None
        return self.page.url

    def cede_to_human(self) -> None:
        self.lock = ControlLock.human

    def pause(self) -> None:
        self.lock = ControlLock.paused

    def return_to_agent(self) -> None:
        self.lock = ControlLock.agent

    def can_act(self) -> bool:
        return self.lock is ControlLock.agent

    def close(self) -> None:
        if self.browser:
            self.browser.close()
        if self._pw:
            self._pw.stop()
        self.browser = None
        self.page = None
        self._pw = None
