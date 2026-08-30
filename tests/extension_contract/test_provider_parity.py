"""Provider-content parity and artifact-masking regressions."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
BROWSERS = {
    name: REPO_ROOT / "extensions" / f"{name}-src" / "content"
    for name in ("chrome", "firefox", "safari")
}
PROVIDER_FILES = (
    "prompt-builder.js",
    "site-gemini.js",
    "site-chatgpt.js",
    "site-perplexity.js",
)
NODE = shutil.which("node")


def run_node(script: str, *paths: Path) -> dict:
    completed = subprocess.run(
        [NODE, "-e", script, *map(str, paths)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout)


class ProviderSourceParityTests(unittest.TestCase):
    def test_provider_neutral_sources_are_byte_identical(self) -> None:
        for filename in PROVIDER_FILES:
            canonical = (BROWSERS["firefox"] / filename).read_bytes()
            for browser in ("chrome", "safari"):
                with self.subTest(browser=browser, filename=filename):
                    self.assertEqual(
                        canonical,
                        (BROWSERS[browser] / filename).read_bytes(),
                    )

    @unittest.skipUnless(NODE, "Node.js is required for JavaScript syntax checks")
    def test_provider_sources_parse_in_all_browser_trees(self) -> None:
        for browser, root in BROWSERS.items():
            for filename in PROVIDER_FILES:
                with self.subTest(browser=browser, filename=filename):
                    completed = subprocess.run(
                        [NODE, "--check", str(root / filename)],
                        cwd=REPO_ROOT,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertEqual(0, completed.returncode, completed.stderr)


@unittest.skipUnless(NODE, "Node.js is required for JavaScript behavior tests")
class ProviderBehaviorTests(unittest.TestCase):
    def test_prompt_builder_wraps_and_strips_user_context(self) -> None:
        script = textwrap.dedent(
            r"""
            const fs = require("node:fs");
            const vm = require("node:vm");
            const context = vm.createContext({ window: {} });
            vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), context);
            const builder = context.window.BdbmPromptBuilder;
            const built = builder.buildEnrichedPrompt({
              userText: "Visible user question",
              memories: [{
                user: "private user memory",
                model: "private model memory",
                turn_distance: 2,
                confidence: 0.9
              }]
            });
            process.stdout.write(JSON.stringify({
              opensContext: built.combinedPrompt.startsWith("<user_context>\n"),
              closesBeforeUser: built.combinedPrompt.includes(
                "</user_context>\n\nVisible user question"
              ),
              detectsContext: builder.containsControlArtifacts(built.combinedPrompt),
              extracted: builder.extractUserPrompt(built.combinedPrompt),
              stripped: builder.stripSystemArtifacts(
                "<user_context><current_time>private</current_time></user_context>Visible"
              )
            }));
            """
        )
        result = run_node(script, BROWSERS["chrome"] / "prompt-builder.js")
        self.assertTrue(result["opensContext"])
        self.assertTrue(result["closesBeforeUser"])
        self.assertTrue(result["detectsContext"])
        self.assertEqual("Visible user question", result["extracted"])
        self.assertEqual("Visible", result["stripped"])

    def test_chatgpt_and_perplexity_detect_new_and_legacy_leaks(self) -> None:
        script = textwrap.dedent(
            r"""
            const fs = require("node:fs");
            const vm = require("node:vm");
            let source = fs.readFileSync(process.argv[1], "utf8");
            source = source.replace(
              "  const adapter = {",
              "  window.__looksLikeLeftoverEnriched = looksLikeLeftoverEnriched;\n\n  const adapter = {"
            );
            const window = {
              BdbmPromptBuilder: null,
              biomemInjector: { init() {} }
            };
            const context = vm.createContext({
              Node: {
                DOCUMENT_POSITION_FOLLOWING: 4,
                DOCUMENT_POSITION_PRECEDING: 2
              },
              document: {
                querySelector() { return null; },
                querySelectorAll() { return []; }
              },
              localStorage: {
                length: 0,
                getItem() { return null; },
                key() { return null; },
                removeItem() {}
              },
              setInterval() { return 1; },
              window
            });
            vm.runInContext(source, context);
            const detects = window.__looksLikeLeftoverEnriched;
            process.stdout.write(JSON.stringify({
              userContext: detects("<user_context>private</user_context>"),
              currentSummary: detects("1. Summary of my query"),
              legacySummary: detects("1. Summary of the USER'S QUERY"),
              plain: detects("ordinary persisted draft")
            }));
            """
        )
        for provider in ("site-chatgpt.js", "site-perplexity.js"):
            with self.subTest(provider=provider):
                result = run_node(script, BROWSERS["chrome"] / provider)
                self.assertTrue(result["userContext"])
                self.assertTrue(result["currentSummary"])
                self.assertTrue(result["legacySummary"])
                self.assertFalse(result["plain"])

    def test_chatgpt_refire_uses_live_react_button_not_inert_form_submit(self) -> None:
        script = textwrap.dedent(
            r"""
            const fs = require("node:fs");
            const vm = require("node:vm");
            let source = fs.readFileSync(process.argv[1], "utf8");
            source = source.replace(
              "  window.biomemInjector.init(adapter);",
              "  window.__chatgptAdapter = adapter;\n  window.biomemInjector.init(adapter);"
            );
            const calls = [];
            const staleButton = { click() { calls.push("stale-click"); } };
            let liveButton;
            const liveForm = {
              requestSubmit(button) {
                calls.push(button === liveButton ? "form-submit-live" : "form-submit-other");
              }
            };
            liveButton = {
              disabled: false,
              getAttribute() { return "false"; },
              form: liveForm,
              click() { calls.push("live-click"); }
            };
            const input = { dispatchEvent() { calls.push("enter"); } };
            const document = {
              querySelector(selector) {
                if (selector.includes("send-button")) return liveButton;
                if (selector.includes("prompt-textarea")) return input;
                return null;
              },
              querySelectorAll() { return []; }
            };
            const window = {
              BdbmPromptBuilder: null,
              biomemInjector: { init() {} }
            };
            const context = vm.createContext({
              Event: class {},
              InputEvent: class {},
              KeyboardEvent: class {},
              Node: { DOCUMENT_POSITION_FOLLOWING: 4, DOCUMENT_POSITION_PRECEDING: 2 },
              document,
              localStorage: { length: 0, getItem() { return null; }, key() { return null; }, removeItem() {} },
              setInterval() { return 1; },
              setTimeout(callback, delay) { calls.push(`wait-${delay}`); callback(); return 1; },
              window
            });
            vm.runInContext(source, context);
            window.__chatgptAdapter.refireAfterSend(input, staleButton, () => calls.push("bypass"))
              .then(() => process.stdout.write(JSON.stringify(calls)));
            """
        )
        result = run_node(script, BROWSERS["chrome"] / "site-chatgpt.js")
        self.assertIn("wait-75", result)
        self.assertIn("bypass", result)
        self.assertIn("live-click", result)
        self.assertNotIn("form-submit-live", result)
        self.assertNotIn("form-submit-other", result)
        self.assertNotIn("stale-click", result)

    def test_chatgpt_refire_rejects_remounted_or_foreign_composer(self) -> None:
        script = textwrap.dedent(
            r"""
            const fs = require("node:fs");
            const vm = require("node:vm");
            let source = fs.readFileSync(process.argv[1], "utf8");
            source = source.replace(
              "  window.biomemInjector.init(adapter);",
              "  window.__chatgptAdapter = adapter;\n  window.biomemInjector.init(adapter);"
            );
            const expected = "<user_context>memory</user_context>\n\nVisible prompt";
            const ownForm = {};
            let clicks = 0;
            let currentInput = {
              innerText: "Visible prompt",
              textContent: "Visible prompt",
              closest(selector) { return selector === "form" ? ownForm : null; },
              dispatchEvent() {}
            };
            let currentButton = {
              disabled: false,
              form: ownForm,
              isConnected: true,
              click() { clicks += 1; },
              getAttribute() { return "false"; }
            };
            const document = {
              querySelector(selector) {
                if (selector.includes("prompt-textarea") || selector.includes("ProseMirror")) return currentInput;
                if (selector.includes("send-button") || selector.includes("Send prompt")) return currentButton;
                return null;
              },
              querySelectorAll() { return []; }
            };
            const window = {
              BdbmPromptBuilder: null,
              biomemInjector: { init() {} }
            };
            const context = vm.createContext({
              Event: class {}, InputEvent: class {}, KeyboardEvent: class {},
              Node: { DOCUMENT_POSITION_FOLLOWING: 4, DOCUMENT_POSITION_PRECEDING: 2 },
              document,
              localStorage: { length: 0, getItem() { return null; }, key() { return null; }, removeItem() {} },
              setInterval() { return 1; },
              setTimeout(callback) { callback(); return 1; },
              window
            });
            vm.runInContext(source, context);
            (async () => {
              const mismatchResult = await window.__chatgptAdapter.refireAfterSend(
                currentInput, currentButton, () => {}, expected
              );
              const mismatchClicks = clicks;
              clicks = 0;
              currentInput = {
                innerText: expected,
                textContent: expected,
                closest(selector) { return selector === "form" ? ownForm : null; },
                dispatchEvent() {}
              };
              currentButton = {
                disabled: false,
                form: null,
                isConnected: true,
                click() { clicks += 1; },
                getAttribute() { return "false"; }
              };
              const foreignResult = await window.__chatgptAdapter.refireAfterSend(
                currentInput, currentButton, () => {}, expected
              );
              process.stdout.write(JSON.stringify({
                foreignClicks: clicks,
                foreignResult,
                mismatchClicks,
                mismatchResult
              }));
            })();
            """
        )
        for browser, root in BROWSERS.items():
            result = run_node(script, root / "site-chatgpt.js")
            with self.subTest(browser=browser, case="remounted-text-mismatch"):
                self.assertFalse(result["mismatchResult"])
                self.assertEqual(0, result["mismatchClicks"])
            with self.subTest(browser=browser, case="foreign-null-form"):
                self.assertFalse(result["foreignResult"])
                self.assertEqual(0, result["foreignClicks"])

    def test_chatgpt_refire_times_out_when_streaming_was_already_active(self) -> None:
        script = textwrap.dedent(
            r"""
            const fs = require("node:fs");
            const vm = require("node:vm");
            let source = fs.readFileSync(process.argv[1], "utf8");
            source = source.replace(
              "  window.biomemInjector.init(adapter);",
              "  window.__chatgptAdapter = adapter;\n  window.biomemInjector.init(adapter);"
            );
            const expected = "<user_context>memory</user_context>\n\nVisible prompt";
            const form = {};
            let clicks = 0;
            const input = {
              innerText: expected,
              textContent: expected,
              closest(selector) { return selector === "form" ? form : null; },
              dispatchEvent() {}
            };
            const button = {
              disabled: false,
              form,
              isConnected: true,
              click() { clicks += 1; },
              getAttribute() { return "false"; }
            };
            const stopButton = {};
            const document = {
              querySelector(selector) {
                if (selector.includes("stop-button") || selector.includes("Stop generating")) return stopButton;
                if (selector.includes("prompt-textarea") || selector.includes("ProseMirror")) return input;
                if (selector.includes("send-button") || selector.includes("Send prompt")) return button;
                return null;
              },
              querySelectorAll() { return []; }
            };
            const window = { BdbmPromptBuilder: null, biomemInjector: { init() {} } };
            const context = vm.createContext({
              Event: class {}, InputEvent: class {}, KeyboardEvent: class {},
              Node: { DOCUMENT_POSITION_FOLLOWING: 4, DOCUMENT_POSITION_PRECEDING: 2 },
              document,
              localStorage: { length: 0, getItem() { return null; }, key() { return null; }, removeItem() {} },
              setInterval() { return 1; },
              setTimeout(callback) { callback(); return 1; },
              window
            });
            vm.runInContext(source, context);
            window.__chatgptAdapter.refireAfterSend(input, button, () => {}, expected)
              .then((result) => process.stdout.write(JSON.stringify({ clicks, result })));
            """
        )
        for browser, root in BROWSERS.items():
            with self.subTest(browser=browser):
                result = run_node(script, root / "site-chatgpt.js")
                self.assertFalse(result["result"])
                self.assertEqual(0, result["clicks"])

    def test_chatgpt_writer_uses_one_idempotent_prosemirror_insertion(self) -> None:
        script = textwrap.dedent(
            r"""
            const fs = require("node:fs");
            const vm = require("node:vm");
            let source = fs.readFileSync(process.argv[1], "utf8");
            source = source.replace(
              "  window.biomemInjector.init(adapter);",
              "  window.__chatgptAdapter = adapter;\n  window.biomemInjector.init(adapter);"
            );
            let insertions = 0;
            const input = {
              innerText: "visible prompt",
              textContent: "visible prompt",
              focus() {},
            };
            const selection = { removeAllRanges() {}, addRange() {} };
            const document = {
              createRange() { return { selectNodeContents() {} }; },
              execCommand(command, _showUi, value) {
                if (command === "insertText") {
                  insertions += 1;
                  input.innerText = value;
                  input.textContent = value;
                }
                return true;
              },
              querySelector() { return null; },
              querySelectorAll() { return []; }
            };
            const window = {
              BdbmPromptBuilder: null,
              biomemInjector: { init() {} },
              getSelection() { return selection; }
            };
            const context = vm.createContext({
              Node: { DOCUMENT_POSITION_FOLLOWING: 4, DOCUMENT_POSITION_PRECEDING: 2 },
              document,
              localStorage: { length: 0, getItem() { return null; }, key() { return null; }, removeItem() {} },
              setInterval() { return 1; },
              window
            });
            vm.runInContext(source, context);
            const writer = window.__chatgptAdapter.writeInputValue;
            writer(input, "enriched prompt");
            writer(input, "enriched prompt");
            process.stdout.write(JSON.stringify({ insertions, text: input.textContent }));
            """
        )
        result = run_node(script, BROWSERS["chrome"] / "site-chatgpt.js")
        self.assertEqual(1, result["insertions"])
        self.assertEqual("enriched prompt", result["text"])

    def test_chatgpt_streaming_detector_tracks_the_live_stop_control(self) -> None:
        script = textwrap.dedent(
            r"""
            const fs = require("node:fs");
            const vm = require("node:vm");
            let source = fs.readFileSync(process.argv[1], "utf8");
            source = source.replace(
              "  window.biomemInjector.init(adapter);",
              "  window.__chatgptAdapter = adapter;\n  window.biomemInjector.init(adapter);"
            );
            let streaming = true;
            const stopButton = {};
            const document = {
              querySelector(selector) {
                if (selector.includes("stop-button")) return streaming ? stopButton : null;
                return null;
              },
              querySelectorAll() { return []; }
            };
            const window = {
              BdbmPromptBuilder: null,
              biomemInjector: { init() {} }
            };
            const context = vm.createContext({
              Node: { DOCUMENT_POSITION_FOLLOWING: 4, DOCUMENT_POSITION_PRECEDING: 2 },
              document,
              localStorage: { length: 0, getItem() { return null; }, key() { return null; }, removeItem() {} },
              setInterval() { return 1; },
              window
            });
            vm.runInContext(source, context);
            const during = window.__chatgptAdapter.isResponseStreaming();
            streaming = false;
            const after = window.__chatgptAdapter.isResponseStreaming();
            process.stdout.write(JSON.stringify({ during, after }));
            """
        )
        result = run_node(script, BROWSERS["chrome"] / "site-chatgpt.js")
        self.assertTrue(result["during"])
        self.assertFalse(result["after"])

    def test_chatgpt_does_not_treat_generic_markdown_as_an_assistant_turn(self) -> None:
        script = textwrap.dedent(
            r"""
            const fs = require("node:fs");
            const vm = require("node:vm");
            const source = fs.readFileSync(process.argv[1], "utf8");
            const genericMarkdown = {
              innerText: "Unable to connect\nRetry",
              matches(selector) { return selector.trim() === "div.markdown"; }
            };
            const document = {
              querySelector() { return null; },
              querySelectorAll(selector) {
                return selector.split(",").some((part) => genericMarkdown.matches(part))
                  ? [genericMarkdown]
                  : [];
              }
            };
            let observedAssistantTurns = null;
            let requiresAuthoritativeAssistantProvenance = null;
            const window = {
              BdbmPromptBuilder: null,
              biomemInjector: {
                init(adapter) {
                  observedAssistantTurns = adapter.getAssistantMessageElements().length;
                  requiresAuthoritativeAssistantProvenance =
                    adapter.requiresAuthoritativeAssistantProvenance === true;
                }
              }
            };
            const context = vm.createContext({
              Node: { DOCUMENT_POSITION_FOLLOWING: 4, DOCUMENT_POSITION_PRECEDING: 2 },
              document,
              localStorage: { length: 0, getItem() { return null; }, key() { return null; }, removeItem() {} },
              setInterval() { return 1; },
              window
            });
            vm.runInContext(source, context);
            process.stdout.write(JSON.stringify({
              observedAssistantTurns,
              requiresAuthoritativeAssistantProvenance
            }));
            """
        )
        result = run_node(script, BROWSERS["chrome"] / "site-chatgpt.js")
        self.assertEqual(0, result["observedAssistantTurns"])
        self.assertTrue(result["requiresAuthoritativeAssistantProvenance"])

    def test_gemini_hidden_label_does_not_consume_visible_replacement(self) -> None:
        script = textwrap.dedent(
            r"""
            const fs = require("node:fs");
            const vm = require("node:vm");
            let source = fs.readFileSync(process.argv[1], "utf8");
            source = source.replace(
              "  const adapter = {",
              "  window.__replaceTextDeep = replaceTextDeep;\n\n  const adapter = {"
            );
            const root = {
              nodeType: 1,
              isConnected: true,
              innerText: "",
              shadowRoot: null,
              closest() { return null; },
              getBoundingClientRect() { return { width: 100, height: 20 }; },
              querySelectorAll() { return []; }
            };
            const srParent = {
              nodeType: 1,
              className: "cdk-visually-hidden",
              parentElement: root,
              getAttribute() { return null; }
            };
            const visibleParent = {
              nodeType: 1,
              className: "visible-copy",
              parentElement: root,
              getAttribute() { return null; }
            };
            const remnantParent = {
              nodeType: 1,
              className: "visible-remnant",
              parentElement: root,
              getAttribute() { return null; }
            };
            const nodes = [
              { nodeValue: "<user_context>private", parentElement: srParent },
              { nodeValue: "<user_context>private", parentElement: visibleParent },
              { nodeValue: "private remnant", parentElement: remnantParent }
            ];
            const document = {
              body: root,
              createTreeWalker() {
                let index = 0;
                return { nextNode() { return nodes[index++] || null; } };
              }
            };
            root.ownerDocument = document;
            const window = {
              biomemInjector: { init() {} },
              getComputedStyle() {
                return {
                  clip: "auto",
                  display: "block",
                  opacity: "1",
                  position: "static",
                  visibility: "visible"
                };
              }
            };
            const context = vm.createContext({
              NodeFilter: { SHOW_TEXT: 4 },
              document,
              window
            });
            vm.runInContext(source, context);
            window.__replaceTextDeep(root, "Visible user question");
            process.stdout.write(JSON.stringify(nodes.map((node) => node.nodeValue)));
            """
        )
        values = run_node(script, BROWSERS["chrome"] / "site-gemini.js")
        self.assertEqual(
            ["Visible user question", "Visible user question", ""],
            values,
        )


if __name__ == "__main__":
    unittest.main()
