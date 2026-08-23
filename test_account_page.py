"""Guards for the account page's render path.

<md-dialog> is defined by the @material/web bundle, which loads from a CDN. The
page's classic script runs and calls render() long before that module lands, so
any custom-element method called from the render path throws on an un-upgraded
element. renderNotice() runs first inside render(), so such a throw takes down
every panel below it -- the page then stays blank until something calls render()
again, and the throw inside refreshData()'s finally also rejects its promise so
handleFragmentState() (which opens the merge dialog) never runs.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


WEB_ROOT = Path(__file__).resolve().parent / "app" / "web"
ACCOUNT_HTML = WEB_ROOT / "account.html"


class AccountPageDialogGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = ACCOUNT_HTML.read_text(encoding="utf-8")

    def test_dialog_open_close_goes_through_the_guarded_helper(self) -> None:
        self.assertIn("function setMergeDialogOpen(", self.html)

        direct_calls = re.findall(
            r'getElementById\(\s*["\']mergeDialog["\']\s*\)\s*\.\s*(?:show|close)\s*\(',
            self.html,
        )
        self.assertEqual(
            direct_calls,
            [],
            "call setMergeDialogOpen() instead: show()/close() on the raw element "
            "throws before the @material/web module upgrades it",
        )

    def test_helper_waits_for_the_custom_element_to_upgrade(self) -> None:
        helper = self._helper_source()
        self.assertIn('customElements.whenDefined("md-dialog")', helper)
        self.assertRegex(
            helper,
            r'typeof\s+dialog\.(?:show|close)\s*!==\s*["\']function["\']',
            "the helper must detect the un-upgraded element before calling it",
        )

    def test_render_notice_no_longer_touches_the_element_directly(self) -> None:
        notice = self._function_source("renderNotice")
        self.assertIn("setMergeDialogOpen(true)", notice)
        self.assertIn("setMergeDialogOpen(false)", notice)
        self.assertNotIn("mergeDialog.show()", notice)
        self.assertNotIn("mergeDialog.close()", notice)

    def test_render_still_runs_the_panels_after_the_notice(self) -> None:
        # Ordering is what made a single throw blank the page; keep it visible.
        render = self._function_source("render")
        for panel in (
            "renderNotice()",
            "renderAccountMetrics()",
            "renderIdentities()",
            "renderCurrentDevice()",
            "renderDevices()",
            "renderButtons()",
        ):
            self.assertIn(panel, render)

    def _helper_source(self) -> str:
        return self._function_source("setMergeDialogOpen")

    def _function_source(self, name: str) -> str:
        marker = f"function {name}("
        start = self.html.index(marker)
        # Walk braces from the opening brace of the function body.
        body_start = self.html.index("{", self.html.index(")", start))
        depth = 0
        for index in range(body_start, len(self.html)):
            char = self.html[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return self.html[start : index + 1]
        raise AssertionError(f"unbalanced braces while reading {name}")


if __name__ == "__main__":
    unittest.main()
