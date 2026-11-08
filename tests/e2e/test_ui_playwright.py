from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "artifacts" / "ui-audit"
VIEWPORTS = [
    {"width": 1440, "height": 900, "label": "desktop"},
    {"width": 1920, "height": 1080, "label": "wide"},
    {"width": 1366, "height": 768, "label": "laptop-1366"},
    {"width": 1280, "height": 720, "label": "laptop-1280"},
    {"width": 1024, "height": 768, "label": "tablet-landscape"},
    {"width": 768, "height": 1024, "label": "tablet-portrait"},
    {"width": 390, "height": 844, "label": "mobile"},
    {"width": 375, "height": 667, "label": "mobile-small"},
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def assert_layout(page, viewport_label: str) -> None:
    state = page.evaluate("""() => {
      const visible = el => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
      };
      const controls = [...document.querySelectorAll('main button, main input, main select, main textarea')]
        .filter(visible)
        .map(el => {
          const r = el.getBoundingClientRect();
          return {tag: el.tagName, id: el.id, name: el.getAttribute('name'), x:r.x, y:r.y, right:r.right, bottom:r.bottom};
        });
      const overlaps = [];
      for (let i=0;i<controls.length;i++) for (let j=i+1;j<controls.length;j++) {
        const a=controls[i], b=controls[j];
        const iw=Math.min(a.right,b.right)-Math.max(a.x,b.x);
        const ih=Math.min(a.bottom,b.bottom)-Math.max(a.y,b.y);
        if (iw>3 && ih>3) overlaps.push([a,b]);
      }
      const unlabeled = [...document.querySelectorAll('main button')].filter(visible)
        .filter(el => !(el.innerText.trim() || el.getAttribute('aria-label')));
      const brokenSvg = [...document.querySelectorAll('svg')].filter(visible)
        .filter(el => !el.getAttribute('viewBox') || el.getBoundingClientRect().width < 4 || el.getBoundingClientRect().height < 4);
      const fontSizes = {
        body: parseFloat(getComputedStyle(document.body).fontSize),
        controls: [...document.querySelectorAll('main input:not([type=checkbox]):not([type=radio]), main select, main textarea')]
          .filter(visible).map(el => parseFloat(getComputedStyle(el).fontSize)),
      };
      const rgb = value => value.startsWith('#')
        ? value.slice(1).match(/.{2}/g).slice(0,3).map(v => parseInt(v,16))
        : value.match(/[\\d.]+/g).slice(0,3).map(Number);
      const luminance = value => {
        const [r,g,b] = rgb(value).map(v => v/255).map(v => v <= .04045 ? v/12.92 : ((v+.055)/1.055)**2.4);
        return .2126*r + .7152*g + .0722*b;
      };
      const contrast = (a,b) => {
        const values = [luminance(a), luminance(b)].sort((x,y) => y-x);
        return (values[0]+.05)/(values[1]+.05);
      };
      const tokens = getComputedStyle(document.documentElement);
      const lightContrast = {
        muted: contrast(tokens.getPropertyValue('--muted').trim(), tokens.getPropertyValue('--paper').trim()),
        terra: contrast(tokens.getPropertyValue('--terra').trim(), tokens.getPropertyValue('--paper').trim()),
        graphText: contrast(tokens.getPropertyValue('--graph-text').trim(), tokens.getPropertyValue('--graph-bg').trim()),
        graphMuted: contrast(tokens.getPropertyValue('--graph-muted').trim(), tokens.getPropertyValue('--graph-bg').trim()),
        champagne: contrast(tokens.getPropertyValue('--champagne').trim(), tokens.getPropertyValue('--graph-bg').trim()),
      };
      const overflowers = [...document.querySelectorAll('body *')].filter(visible).map(el => {
        const r=el.getBoundingClientRect();
        return {tag:el.tagName, id:el.id, className:typeof el.className==='string'?el.className:'', text:(el.innerText||'').slice(0,60), x:r.x,right:r.right,width:r.width,scrollWidth:el.scrollWidth};
      }).filter(x=>x.x < -1 || x.right > innerWidth + 1 || x.scrollWidth > x.width + 2).slice(0,15);
      return {
        width: innerWidth,
        docWidth: document.documentElement.scrollWidth,
        bodyWidth: document.body.scrollWidth,
        overlaps,
        unlabeled: unlabeled.map(el => el.outerHTML),
        brokenSvg: brokenSvg.map(el => el.outerHTML),
        fontSizes,
        contrast: lightContrast,
        fonts: {
          manrope: document.fonts.check('400 16px Manrope', 'Кириллица'),
          cormorant: document.fonts.check('600 24px "Cormorant Garamond"', 'Кириллица'),
        },
        overflowers
      };
    }""")
    assert state["docWidth"] <= state["width"] + 1, f"horizontal overflow at {viewport_label}: {state}"
    assert state["bodyWidth"] <= state["width"] + 1, f"body overflow at {viewport_label}: {state}"
    assert not state["overlaps"], f"interactive elements overlap at {viewport_label}: {state['overlaps']}"
    assert not state["unlabeled"], f"unlabeled buttons at {viewport_label}: {state['unlabeled']}"
    assert not state["brokenSvg"], f"broken SVG icon at {viewport_label}: {state['brokenSvg']}"
    assert state["fontSizes"]["body"] >= 16, f"body text below 16px at {viewport_label}: {state['fontSizes']}"
    assert all(size >= 16 for size in state["fontSizes"]["controls"]), f"form control text below 16px at {viewport_label}: {state['fontSizes']}"
    assert all(ratio >= 4.5 for ratio in state["contrast"].values()), f"text contrast below 4.5:1 at {viewport_label}: {state['contrast']}"


class SlayBrowserAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.font_observations = []
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="slay-ui-audit-")
        cls.port = free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        env = os.environ.copy()
        env["SPIRIT_LLM_API_KEY"] = ""
        env["PORT"] = str(cls.port)
        env["SLAY_DB_PATH"] = str(Path(cls.temp_dir.name) / "audit.sqlite3")
        cls.server = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py")],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(80):
            if cls.server.poll() is not None:
                raise RuntimeError(f"Backend exited before startup with status {cls.server.returncode}")
            try:
                urllib.request.urlopen(cls.base_url + "/api/me", timeout=1).close()
                break
            except (OSError, urllib.error.URLError):
                time.sleep(.15)
        else:
            raise RuntimeError("Backend did not become ready")

        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None):
            cls.browser.close()
        if getattr(cls, "playwright", None):
            cls.playwright.stop()
        if getattr(cls, "server", None):
            cls.server.terminate()
            try:
                cls.server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.server.kill()
        if getattr(cls, "temp_dir", None):
            cls.temp_dir.cleanup()

    def page_for(self, viewport):
        context = self.browser.new_context(viewport={"width": viewport["width"], "height": viewport["height"]})
        page = context.new_page()
        # Keep browser checks deterministic and independent of external font CDN availability.
        page.route("https://fonts.googleapis.com/**", lambda route: route.fulfill(status=200, content_type="text/css", body=""))
        page.set_default_timeout(7000)
        page.set_default_navigation_timeout(20000)
        page.emulate_media(reduced_motion="reduce")
        console_errors = []
        page.on("pageerror", lambda error: console_errors.append(str(error)))
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        return context, page, console_errors

    def screenshot(self, page, viewport, state):
        name = f"{viewport['label']}-{state}.png"
        page.screenshot(path=ARTIFACTS / name, full_page=True)
        fonts = page.evaluate("""async () => {
          await document.fonts.ready;
          return {
            status: document.fonts.status,
            manropeCyrillic: document.fonts.check('400 16px Manrope', 'Кириллица'),
            cormorantCyrillic: document.fonts.check('600 24px Cormorant Garamond', 'Кириллица'),
          };
        }""")
        self.__class__.font_observations.append({"viewport": viewport["label"], "screen": state, **fonts})
        (ARTIFACTS / "font-load-status.json").write_text(
            json.dumps(self.__class__.font_observations, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        assert_layout(page, viewport["label"])

    def complete_tutorial(self, page, viewport):
        expect(page.locator("#tutorial-form")).to_be_visible()
        page.screenshot(path=ARTIFACTS / f"{viewport['label']}-tutorial.png", full_page=True)
        assert_layout(page, viewport["label"])
        page.locator('input[name="ok"]').check()
        page.locator("#tutorial-form button[type=submit]").click()
        expect(page.locator("#tutorial-chart svg")).to_be_visible()
        page.screenshot(path=ARTIFACTS / f"{viewport['label']}-tutorial-chart.png", full_page=True)
        assert_layout(page, viewport["label"])
        page.locator('input[name="answer"][value="yes"]').check()
        page.locator("#tutorial-form button[type=submit]").click()
        expect(page.locator(".tutorial-head")).to_contain_text("03 / 04")
        page.locator('input[name="answer"][value="yes"]').check()
        page.locator("#tutorial-form button[type=submit]").click()
        expect(page.locator("#tutorial-practice-form")).to_be_visible()
        page.screenshot(path=ARTIFACTS / f"{viewport['label']}-tutorial-practice.png", full_page=True)
        assert_layout(page, viewport["label"])
        page.locator('select[name="company"]').select_option(index=1)
        page.locator('select[name="year"]').select_option(index=1)
        page.locator('select[name="month"]').select_option(index=1)
        page.locator('input[name="forecast"][value="flat"]').check()
        page.locator('textarea[name="rationale"]').fill("Compare market context and uncertainty carefully.")
        page.locator('input[name="strategy"][value="hold"]').check()
        page.locator('input[name="factor"]').first.check()
        for quiz in page.locator("fieldset.quiz-question").all():
            quiz.locator("input[type=radio]").first.check()
        page.locator("#tutorial-practice-submit").click()
        expect(page.locator("#finish-tutorial")).to_be_visible()
        self.screenshot(page, viewport, "tutorial-practice-result")
        practice_profile = page.request.get(self.base_url + "/api/profile").json()
        self.assertEqual(practice_profile["stats"]["rounds"], 0, "tutorial practice must not enter user statistics")
        page.locator("#finish-tutorial").click()
        expect(page.locator(".level-card[data-level=easy]")).to_be_visible()
    def complete_round(self, page):
        if not page.locator("#answer-form").is_visible():
            page.locator('[data-panel="decision"]').click()
        expect(page.locator("#answer-form")).to_be_visible()
        if page.locator(".game-tabs").is_visible():
            page.locator('[data-panel="investigation"]').click()
        if page.viewport_size["width"] <= 640:
            page.locator("#round-chart").click(position={"x": 160, "y": 120})
        else:
            page.locator("#round-chart").hover(position={"x": 160, "y": 120})
        expect(page.locator("#chart-tooltip")).to_be_visible()
        self.assertIn("Торговый день", page.locator("#chart-tooltip").inner_text())
        self.assertNotRegex(page.locator("#chart-tooltip").inner_text(), r"\b20\d\d-\d\d-\d\d\b", "exact dates must remain hidden before the answer")
        if page.viewport_size["width"] <= 640:
            page.locator("#round-chart").focus()
            page.keyboard.press("ArrowRight")
            self.assertIn("Торговый день", page.locator("#chart-tooltip").inner_text())
        if page.locator(".game-tabs").is_visible():
            page.locator('[data-panel="decision"]').click()
        page.locator('select[name="company"]').select_option(index=1)
        # The first option is the placeholder; index 1 is the first valid year.
        page.locator('select[name="year"]').select_option(index=1)
        page.locator('select[name="month"]').select_option(index=1)
        page.locator('input[name="forecast"][value="flat"]').check()
        page.locator('textarea[name="rationale"]').fill("Проверяю сценарий, рыночный фон и степень неопределённости.")
        page.locator('input[name="strategy"][value="hold"]').check()
        page.locator('input[name="factor"]').first.check()
        for quiz in page.locator("fieldset.quiz-question").all():
            quiz.locator("input[type=radio]").first.check()
        if page.locator(".game-tabs").is_visible():
            page.locator('[data-panel="investigation"]').click()
            page.locator('[data-panel="decision"]').click()
            self.assertEqual(page.locator('textarea[name="rationale"]').input_value(), "Проверяю сценарий, рыночный фон и степень неопределённости.")
        if page.locator(".game-tabs").is_visible():
            page.locator('[data-panel="investigation"]').click()
        page.locator("#open-hint").click()
        expect(page.locator(".hint-card")).to_have_count(1)
        if page.locator(".game-tabs").is_visible():
            page.locator('[data-panel="decision"]').click()
        page.reload()
        expect(page.locator(".hint-card")).to_have_count(1)
        if not page.locator("#answer-form").is_visible():
            page.locator('[data-panel="decision"]').click()
        button = page.locator("#lock-answer")
        expect(button).to_be_enabled()
        button.click()
        expect(page.locator(".result-banner")).to_be_visible()

    def test_registration_errors_and_form_state(self):
        context, page, errors = self.page_for(VIEWPORTS[0])
        try:
            page.goto(self.base_url, wait_until="domcontentloaded")
            expect(page.locator("#hero-cta")).to_be_visible()
            page.keyboard.press("Tab")
            focus_style = page.evaluate("""() => {
              const style = getComputedStyle(document.activeElement);
              return {tag: document.activeElement.tagName, outline: style.outlineStyle, width: parseFloat(style.outlineWidth)};
            }""")
            self.assertEqual(focus_style["outline"], "solid", f"keyboard focus is not visible: {focus_style}")
            self.assertGreaterEqual(focus_style["width"], 3, f"keyboard focus ring is too small: {focus_style}")
            page.locator("#hero-cta").click()
            self.screenshot(page, VIEWPORTS[0], "registration")
            form = page.locator("#auth-form")
            expect(form).to_be_visible()

            # Empty fields use native validation and do not send the request.
            page.locator("#auth-form button[type=submit]").click()
            self.assertFalse(form.evaluate("(el) => el.checkValidity()"))

            email = f"ui-audit-{time.time_ns()}@example.test"
            page.locator('input[name="display_name"]').fill("UI Auditor")
            page.locator('input[name="email"]').fill("not-an-email")
            page.locator('input[name="password"]').fill("StrongPass123")
            self.assertTrue(page.locator('input[name="email"]').evaluate("(el) => el.validity.typeMismatch"))

            # Server validation for weak password preserves every entered field.
            page.locator('input[name="email"]').fill(email)
            page.locator('input[name="password"]').fill("weakpass")
            page.locator("#auth-form button[type=submit]").click()
            expect(page.locator("#auth-error")).to_contain_text("Пароль")
            self.assertEqual(page.locator('input[name="display_name"]').input_value(), "UI Auditor")
            self.assertEqual(page.locator('input[name="email"]').input_value(), email)
            self.assertEqual(page.locator('input[name="password"]').input_value(), "weakpass")

            # API 500 and network failure keep the form values in the live DOM.
            page.locator('input[name="password"]').fill("StrongPass123")
            page.route("**/api/auth/register", lambda route: route.fulfill(status=500, content_type="application/json", body=json.dumps({"error":"internal"})))
            page.locator("#auth-form button[type=submit]").click()
            expect(page.locator("#auth-error")).to_contain_text("временно недоступен")
            self.assertEqual(page.locator('input[name="email"]').input_value(), email)
            page.unroute("**/api/auth/register")
            page.route("**/api/auth/register", lambda route: route.abort("failed"))
            page.locator("#auth-form button[type=submit]").click()
            expect(page.locator("#auth-error")).to_contain_text("связаться с сервером")
            self.assertEqual(page.locator('input[name="display_name"]').input_value(), "UI Auditor")
        finally:
            context.close()

    def test_register_login_tutorial_easy_round_result_profile_responsive(self):
        all_console_errors = []
        for viewport in VIEWPORTS:
            context, page, errors = self.page_for(viewport)
            all_console_errors.extend(errors)
            try:
                page.goto(self.base_url, wait_until="domcontentloaded")
                expect(page.locator("#preview-chart svg")).to_be_visible()
                self.screenshot(page, viewport, "landing")
                page.locator("#hero-cta").click()
                self.screenshot(page, viewport, "registration")

                email = f"ui-flow-{viewport['label']}-{time.time_ns()}@example.test"
                password = "PracticePass123"
                page.locator('input[name="display_name"]').fill("Slay Learner")
                page.locator('input[name="email"]').fill(email)
                page.locator('input[name="password"]').fill(password)
                page.locator("#auth-form button[type=submit]").click()
                expect(page.locator("#tutorial-form")).to_be_visible()
                page.reload()
                expect(page.locator("#tutorial-form")).to_be_visible()
                self.complete_tutorial(page, viewport)
                self.screenshot(page, viewport, "levels")

                # Verify the registered session through a fresh login.
                login_context, login_page, login_errors = self.page_for(viewport)
                all_console_errors.extend(login_errors)
                login_page.goto(self.base_url, wait_until="domcontentloaded")
                login_page.locator("#hero-cta").click()
                login_page.locator("#switch-auth").click()
                self.screenshot(login_page, viewport, "login")
                login_page.locator('input[name="email"]').fill(email)
                login_page.locator('input[name="password"]').fill(password)
                login_page.locator("#auth-form button[type=submit]").click()
                expect(login_page.locator(".dashboard")).to_be_visible()
                login_page.locator(".level-card[data-level=easy]").click()
                expect(login_page.locator("#answer-form")).to_be_attached()
                expect(login_page.locator(".briefing h2")).to_have_count(0)
                expect(login_page.locator(".briefing")).not_to_contain_text("Когда неопределённость ещё не стала паникой")
                self.assertGreaterEqual(len(login_page.locator(".article-extra p").all()), 1)
                months, months_in, months_genitive, plurals = login_page.evaluate("() => [MONTHS, MONTHS_IN, MONTHS_GENITIVE, [0,1,2,4,5,11,12,14,21,22,25,101].map(pluralTimes)]")
                self.assertEqual(months, ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"])
                self.assertEqual(months_in, ["январе", "феврале", "марте", "апреле", "мае", "июне", "июле", "августе", "сентябре", "октябре", "ноябре", "декабре"])
                self.assertEqual(months_genitive, ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"])
                self.assertEqual(plurals, ["раз", "раз", "раза", "раза", "раз", "раз", "раз", "раз", "раз", "раза", "раз", "раз"])
                valid_periods = login_page.evaluate("() => [...document.querySelector('select[name=year]').options].slice(1).map(option => Number(option.value))")
                self.assertTrue(valid_periods)
                self.assertNotIn(2025, valid_periods, "Do not add an unverified year to the answer catalog")
                if viewport["width"] <= 980:
                    expect(login_page.locator('[data-panel="decision"]')).to_be_visible()
                    login_page.locator('[data-panel="decision"]').click()
                    if viewport["width"] <= 640:
                        track_box = login_page.locator(".game-progress .round-track").bounding_box()
                        meta_box = login_page.locator(".game-progress .game-meta").bounding_box()
                        self.assertLessEqual(track_box["y"] + track_box["height"], meta_box["y"], "round label should appear beneath the square indicators on mobile")
                expect(login_page.locator("#answer-form")).to_be_visible()
                catalog_periods = login_page.evaluate("""async () => {
                  const sid = localStorage.getItem('slay_investments_session');
                  const response = await fetch('/api/session', {headers: {'X-Session-Id': sid}});
                  return (await response.json()).current.periods;
                }""")
                for year in sorted({period[0] for period in catalog_periods}):
                    login_page.locator('select[name="year"]').select_option(value=str(year))
                    visible_months = login_page.locator('select[name="month"] option').evaluate_all("els => els.slice(1).map(el => [Number(el.value), el.textContent])")
                    expected_months = [list(pair) for pair in sorted((month, months[month - 1]) for valid_year, month in catalog_periods if valid_year == year)]
                    self.assertEqual(visible_months, expected_months, f"month options for catalog year {year}")
                self.screenshot(login_page, viewport, "round")
                if login_page.locator(".game-tabs").is_visible():
                    login_page.locator('[data-panel="investigation"]').click()
                login_page.locator("#chart-zoom-in").click()
                self.assertGreater(login_page.locator("#round-chart").evaluate("el => el.scrollWidth"), login_page.locator("#round-chart").evaluate("el => el.clientWidth"))
                login_page.locator("#chart-zoom-reset").click()
                if login_page.locator(".game-tabs").is_visible():
                    login_page.locator('[data-panel="decision"]').click()
                self.complete_round(login_page)
                expect(login_page.locator(".argument-review small")).to_contain_text("Локальная проверка · LLM недоступна")
                self.screenshot(login_page, viewport, "result")
                login_page.locator("#next-round").click()
                login_page.locator("#brand-home").click()
                login_page.locator("#profile-menu-trigger").click()
                login_page.locator("#nav-profile").click()
                expect(login_page.locator(".profile")).to_be_visible()
                expect(login_page.locator(".profile h1")).to_have_text("Мой профиль")
                expect(login_page.locator(".profile")).to_contain_text("Максимальный открытый уровень")
                expect(login_page.locator("#preferences-form")).to_be_visible()
                login_page.locator("#preferences-form").locator("button[type=submit]").click()
                expect(login_page.locator("#preference-status")).to_contain_text("Сохранено")
                self.screenshot(login_page, viewport, "profile")
                # A browser zoom changes the effective CSS viewport. Exercise the
                # equivalent layout widths here; Playwright does not control the
                # browser chrome zoom percentage itself.
                for label, width, height in [("zoom-125-equivalent", 1152, 720), ("zoom-200-equivalent", 720, 450)]:
                    login_page.set_viewport_size({"width": width, "height": height})
                    self.screenshot(login_page, {"width": width, "height": height, "label": label}, "profile")
                login_page.set_viewport_size({"width": viewport["width"], "height": viewport["height"]})
                login_page.locator("#profile-menu-trigger").click()
                login_page.keyboard.press("Escape")
                self.assertEqual(login_page.locator("#profile-menu-trigger").get_attribute("aria-expanded"), "false")
                login_page.locator("#profile-menu-trigger").click()
                login_page.locator("#nav-logout").click()
                expect(login_page.locator("#hero-cta")).to_be_visible()
                self.assertFalse(login_errors, f"browser errors at {viewport['label']}: {login_errors}")
                login_context.close()

                # Existing email must produce an explicit conflict without clearing fields.
                duplicate_context, duplicate_page, duplicate_errors = self.page_for(viewport)
                # External font CSS may delay DOMContentLoaded; wait for the
                # document commit, then assert the local UI is actually ready.
                duplicate_page.goto(self.base_url, wait_until="commit")
                expect(duplicate_page.locator("#hero-cta")).to_be_visible()
                duplicate_page.locator("#hero-cta").click()
                duplicate_page.locator('input[name="display_name"]').fill("Another Learner")
                duplicate_page.locator('input[name="email"]').fill(email)
                duplicate_page.locator('input[name="password"]').fill(password)
                duplicate_page.locator("#auth-form button[type=submit]").click()
                expect(duplicate_page.locator("#auth-error")).to_contain_text("уже существует")
                self.assertEqual(duplicate_page.locator('input[name="email"]').input_value(), email)
                assert_layout(duplicate_page, viewport["label"])
                unexpected_duplicate_errors = [error for error in duplicate_errors if "409 (Conflict)" not in error]
                self.assertFalse(unexpected_duplicate_errors, f"unexpected browser errors on duplicate registration at {viewport['label']}: {unexpected_duplicate_errors}")
                duplicate_context.close()
                self.assertFalse(errors, f"browser errors at {viewport['label']}: {errors}")
            finally:
                context.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)


