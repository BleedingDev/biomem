"""Cross-browser parity tests for browser-neutral content-script behavior."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = Path(__file__).resolve().parent
BROWSERS = {
    name: REPO_ROOT / "extensions" / f"{name}-src" / "content" / "common.js"
    for name in ("chrome", "firefox", "safari")
}
NODE = shutil.which("node")


FEATURE_SECTIONS = {
    "control-artifact detection": ("  function containsControlArtifacts", "  function extractUserPrompt"),
    "message replacement diagnostics": ("  function replaceMessageText", "  function applyReactSafeOverlay"),
    "context stripping": ("  function surgicalRemovePamTokens", "  function getUserMessageCount"),
    "queue diagnostics": ("  function enqueuePendingUser", "  function needsUserMask"),
    "timestamp and masking diagnostics": ("  function extractPromptTimestamp", "  function schedulePendingUserSweep"),
    "scheduled masking": ("  function schedulePendingUserSweep", "  function isUnsafeMaskTarget"),
    "node leak detection": ("  function findLeakCandidateInNode", "  function getMaskMaxLen"),
    "strong leak detection": ("  function hasStrongSystemLeak", "  function shouldIgnoreAssistantCandidate"),
    "assistant candidate filtering": ("  function shouldIgnoreAssistantCandidate", "  function expirePendingStoreIfStale"),
    "leak scoring": ("  function scoreStrongLeakText", "  function findArtifactLeakInContainer"),
    "visible leak masking": ("  function sanitizeAllVisibleLeakNodes", "  function sanitizeLatestUserLeak"),
    "focused input writing": ("  function setInputValue", "  function triggerSend"),
    "composer send filtering": ("  function attachSendHooks", "  function startUserMessageObserver"),
    "user observer diagnostics": ("  function startUserMessageObserver", "  function startAssistantObserver"),
    "deep Gemini masking": ("  function startDeepLeakSweep", "  async function finalizeAssistant"),
}


def read_normalized(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def section(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index].rstrip()


class NormalizedFeatureParityTests(unittest.TestCase):
    def test_browser_neutral_feature_sections_match(self) -> None:
        sources = {name: read_normalized(path) for name, path in BROWSERS.items()}
        for feature, (start, end) in FEATURE_SECTIONS.items():
            canonical = section(sources["firefox"], start, end)
            self.assertEqual(canonical, section(sources["safari"], start, end), feature)
            self.assertEqual(canonical, section(sources["chrome"], start, end), feature)


@unittest.skipUnless(NODE, "Node.js is required for JavaScript behavior tests")
class CommonBehaviorParityTests(unittest.TestCase):
    def run_scenario(self, browser: str, scenario: str) -> dict:
        completed = subprocess.run(
            [NODE, str(TEST_ROOT / "common_parity_harness.js"), str(BROWSERS[browser]), scenario],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def test_user_context_is_detected_scored_timestamped_and_stripped(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "user_context")
                self.assertTrue(result["controlDetected"])
                self.assertTrue(result["strongLeakDetected"])
                self.assertTrue(result["cleaned"])
                self.assertEqual("before after", result["cleanedText"])
                self.assertEqual("2026-08-27 12:34", result["timestamp"])
                self.assertGreaterEqual(result["leakScore"], 16)

    def test_focus_guard_never_writes_enriched_text_to_login_field(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "focus_guard")
                self.assertEqual("original prompt", result["previous"])
                self.assertEqual("enriched prompt", result["composerText"])
                self.assertEqual("person@example.test", result["loginValue"])
                self.assertEqual(0, result["execCommandCalls"])
                self.assertEqual(["input", "change"], result["inputEvents"])

    def test_handled_paste_is_not_inserted_a_second_time(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "handled_paste")
                self.assertEqual("enriched prompt", result["text"])
                self.assertEqual(0, result["execCommandCalls"])
                self.assertEqual(["paste"], result["events"])

    def test_input_writer_does_not_dispatch_beforeinput_before_exec_command(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "handled_beforeinput")
                self.assertEqual("enriched prompt", result["text"])
                self.assertEqual(0, result["beforeInputCalls"])
                self.assertEqual(1, result["execCommandCalls"])

    def test_login_submit_is_not_treated_as_chat_send(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "composer_guard")
                self.assertFalse(result["prevented"])
                self.assertEqual(0, result["retrieveCalls"])

    def test_assistant_cleanup_is_scoped_to_the_latest_response(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "assistant_cleanup_scope")
                self.assertEqual(
                    ["current-assistant-1", "current-assistant-2"],
                    result["targetIds"],
                )

    def test_cached_enriched_send_is_suppressed_and_refired_once(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "cached_send_refire")
                self.assertEqual(["prevent", "stop", "refire", "clear"], result["calls"])
                self.assertEqual("", result["inputValue"])
                self.assertEqual(4, result["refireArgumentCount"])
                self.assertEqual(
                    "<user_context>cached memory</user_context>\n\nvisible prompt",
                    result["refireExpectedPrompt"],
                )

    def test_candidate_rerank_keeps_opaque_identifier_in_bounded_context(self) -> None:
        for browser in BROWSERS:
            result = self.run_scenario(browser, "candidate_retrieval_rerank")
            with self.subTest(browser=browser, surface="candidate-pool"):
                self.assertEqual(20, result["candidateCount"])
                self.assertEqual(7, result["exactRank"])
                self.assertEqual(20, result["requestedLimit"])
            with self.subTest(browser=browser, surface="final-context"):
                prompt = result["providerPrompt"]
                self.assertIn(result["exactUser"], prompt)
                self.assertIn(result["exactModel"], prompt)
                self.assertNotIn("Unknown.", prompt)
                self.assertLessEqual(prompt.count(" | Model:"), 5)

    def test_failed_enriched_send_preserves_the_original_draft(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "failed_send_keeps_draft")
                self.assertEqual(["prevent", "stop", "refire"], result["calls"])
                self.assertEqual("visible prompt", result["inputValue"])

    def test_pending_user_sweep_cancellation_is_item_scoped(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "pending_user_cancellation_identity")
                self.assertEqual(
                    {"first": True, "second": True},
                    result["beforeCancel"],
                )
                self.assertEqual(
                    {"first": False, "second": True},
                    result["afterCancel"],
                )

    def test_chatgpt_temporary_composer_refires_enriched_prompt_once(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(
                    browser,
                    "public_chatgpt_temporary_composer_send",
                )
                self.assertTrue(result["eventPrevented"])
                self.assertEqual(1, len(result["retrieveCalls"]))
                retrieve_user, retrieve_session, retrieve_limit = result["retrieveCalls"][0]
                self.assertEqual(result["userText"], retrieve_user)
                self.assertTrue(retrieve_session)
                self.assertEqual(20, retrieve_limit)
                self.assertEqual([result["expectedEnriched"]], result["nativeRefires"])
                self.assertEqual(1, result["liveButtonClicks"])
                self.assertEqual(0, result["formRequestSubmitCalls"])
                self.assertEqual("", result["composerText"])
                self.assertEqual(
                    [[
                        result["userText"],
                        result["assistantText"],
                        retrieve_session,
                        result["assistantText"],
                    ]],
                    result["storeCalls"],
                )

    def test_chatgpt_stale_controlled_state_rolls_back_without_pending_turn(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "public_chatgpt_stale_controlled_state")
                self.assertEqual([result["expectedEnriched"]], result["nativeRefires"])
                self.assertEqual([result["userText"]], result["providerConsumedPrompts"])
                self.assertEqual(1, result["liveButtonClicks"])
                self.assertEqual(result["userText"], result["composerText"])
                self.assertFalse(result["pendingActive"])
                self.assertEqual([], result["storeCalls"])

    def test_chatgpt_input_event_syncs_enriched_provider_payload(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "public_chatgpt_input_event_sync")
                self.assertEqual([result["expectedEnriched"]], result["nativeRefires"])
                self.assertEqual(
                    [result["expectedEnriched"]],
                    result["providerConsumedPrompts"],
                )
                self.assertEqual(1, result["liveButtonClicks"])
                self.assertEqual(1, len(result["storeCalls"]))

    def test_chatgpt_unavailable_live_send_restores_original_draft(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(
                    browser,
                    "public_chatgpt_unavailable_composer_send",
                )
                self.assertTrue(result["eventPrevented"])
                self.assertEqual(1, len(result["retrieveCalls"]))
                self.assertEqual([], result["nativeRefires"])
                self.assertEqual(0, result["liveButtonClicks"])
                self.assertEqual(0, result["formRequestSubmitCalls"])
                self.assertEqual(result["userText"], result["composerText"])
                self.assertEqual([], result["storeCalls"])

    def test_chatgpt_inert_enabled_send_times_out_and_rolls_back_without_turn_state(
        self,
    ) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(
                    browser,
                    "public_chatgpt_inert_enabled_composer_send",
                )
                self.assertTrue(result["eventPrevented"])
                self.assertEqual(1, len(result["retrieveCalls"]))
                self.assertEqual([result["expectedEnriched"]], result["nativeRefires"])
                self.assertEqual(1, result["liveButtonClicks"])
                self.assertEqual(0, result["formRequestSubmitCalls"])
                self.assertEqual(0, result["composerDeleteCalls"])
                self.assertEqual(result["userText"], result["composerText"])
                self.assertFalse(result["pendingActive"])
                self.assertEqual([], result["storeCalls"])

    def test_chatgpt_failed_send_sweeps_cannot_corrupt_the_retry_turn(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(
                    browser,
                    "public_chatgpt_failed_send_then_retry",
                )
                self.assertEqual(2, len(result["retrieveCalls"]))
                first_session = result["retrieveCalls"][0][1]
                retry_session = result["retrieveCalls"][1][1]
                self.assertNotEqual(first_session, retry_session)
                self.assertEqual(result["retryUserText"], result["retryBubbleText"])
                self.assertEqual(2, result["liveButtonClicks"])
                self.assertEqual(0, result["formRequestSubmitCalls"])
                self.assertEqual(1, len(result["storeCalls"]))
                self.assertEqual(result["retryUserText"], result["storeCalls"][0][0])
                self.assertEqual(retry_session, result["storeCalls"][0][2])

    def test_stable_assistant_without_pam_is_stored_exactly(self) -> None:
        expected = [
            "What exact phrase does the marker mean?",
            "the violet sextant points south at midnight.",
            "exact-session",
            "the violet sextant points south at midnight.",
        ]
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "exact_store_without_pam")
                self.assertEqual([expected], result["calls"])

    def test_streaming_assistant_without_pam_is_not_stored_early(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(
                    browser, "streaming_without_pam_defers_store"
                )
                self.assertEqual([], result["calls"])

    def test_assistant_that_settles_after_streaming_is_stored(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "public_delayed_assistant_settlement")
                self.assertEqual(0, result["storesBeforeDebounce"])
                self.assertEqual(1, len(result["calls"]))
                self.assertEqual(
                    "A stable answer that appeared after streaming stopped.",
                    result["calls"][0][1],
                )
                self.assertEqual(
                    result["lookupsAtSettlement"],
                    result["lookupsAfterHorizon"],
                )

    def test_assistant_debounce_is_cancelled_and_restarted_by_new_text(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "public_debounce_cancellation")
                self.assertEqual(0, result["callsBeforeFinalDebounce"])
                self.assertEqual(1, len(result["calls"]))
                self.assertEqual(
                    "Final answer after the stream settled.",
                    result["calls"][0][1],
                )

    def test_connection_error_ui_is_not_stored(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "public_reject_connection_error")
                self.assertEqual([], result["calls"])

    def test_security_captcha_and_login_ui_are_not_stored(self) -> None:
        scenarios = (
            "public_reject_security_verification",
            "public_reject_captcha",
            "public_reject_login_ui",
        )
        for browser in BROWSERS:
            for scenario in scenarios:
                with self.subTest(browser=browser, scenario=scenario):
                    result = self.run_scenario(browser, scenario)
                    self.assertEqual([], result["calls"])

    def test_user_prompt_echo_is_not_stored(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "public_reject_user_prompt_echo")
                self.assertEqual([], result["calls"])

    def test_user_prompt_echo_with_pam_markers_is_not_stored(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(
                    browser, "public_reject_pam_user_prompt_echo"
                )
                self.assertEqual([], result["calls"])

    def test_legitimate_answer_using_error_words_is_stored(self) -> None:
        expected = (
            "If your client is unable to connect through Cloudflare, retry with a "
            "fresh network session. A Request ID can help support teams correlate the "
            "diagnostic."
        )
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "public_legitimate_error_words")
                self.assertEqual(1, len(result["calls"]))
                call = result["calls"][0]
                self.assertEqual("Visible user prompt", call[0])
                self.assertEqual(expected, call[1])
                self.assertTrue(call[2])
                self.assertEqual(result["retrieveCalls"][0][1], call[2])
                self.assertEqual(expected, call[3])

    def test_legitimate_answer_using_login_and_security_words_is_stored(
        self,
    ) -> None:
        expected = (
            "To log in or sign up, complete the security verification. If the guide "
            "says Verify you are human, follow the documented steps."
        )
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(
                    browser, "public_legitimate_security_words"
                )
                self.assertEqual(1, len(result["calls"]))
                self.assertEqual(expected, result["calls"][0][1])
                self.assertEqual(expected, result["calls"][0][3])

    def test_provider_shell_mutation_waits_for_authoritative_assistant_turn(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "public_provider_shell_provenance")
                self.assertEqual(0, result["callsAfterProviderShell"])
                self.assertEqual(1, len(result["calls"]))
                self.assertEqual(
                    "The authoritative assistant answer.", result["calls"][0][1]
                )

    def test_repeated_mutations_while_store_is_in_flight_remain_exactly_once(
        self,
    ) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(
                    browser, "public_exact_once_while_store_in_flight"
                )
                self.assertEqual(1, result["callsWhileFirstStoreIsPending"])
                self.assertEqual(1, result["callsBeforeRelease"])
                self.assertEqual(1, len(result["calls"]))

    def test_repeated_mutations_after_resolved_store_remain_exactly_once(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(
                    browser, "public_exact_once_after_resolved_store"
                )
                self.assertEqual(1, result["callsAfterResolvedStore"])
                self.assertEqual(1, len(result["calls"]))

    def test_two_sequential_turns_store_once_each_with_distinct_sessions(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "public_two_sequential_turns")
                self.assertEqual(1, result["callsAfterFirstTurn"])
                self.assertEqual(2, len(result["calls"]))
                first_call, second_call = result["calls"]
                expected = result["expected"]
                self.assertEqual(expected["firstUser"], first_call[0])
                self.assertEqual(expected["firstAssistant"], first_call[1])
                self.assertEqual(expected["firstAssistant"], first_call[3])
                self.assertEqual(expected["secondUser"], second_call[0])
                self.assertEqual(expected["secondAssistant"], second_call[1])
                self.assertEqual(expected["secondAssistant"], second_call[3])
                self.assertNotEqual(first_call[2], second_call[2])

    def test_prior_assistant_is_not_stored_under_the_next_pending_turn(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "public_stale_assistant_across_turns")
                self.assertEqual(1, result["callsWhilePriorAssistantRemains"])
                self.assertEqual(2, len(result["calls"]))
                first_call, second_call = result["calls"]
                expected = result["expected"]
                self.assertEqual(expected["firstUser"], first_call[0])
                self.assertEqual(expected["firstAssistant"], first_call[1])
                self.assertEqual(expected["secondUser"], second_call[0])
                self.assertEqual(expected["secondAssistant"], second_call[1])
                self.assertEqual(expected["secondAssistant"], second_call[3])
                self.assertNotEqual(result["firstSession"], second_call[2])

    def test_identical_assistant_text_stores_once_in_each_distinct_turn(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(
                    browser, "public_identical_assistant_text_across_turns"
                )
                self.assertEqual(1, result["callsAfterFirstTurn"])
                self.assertEqual(2, len(result["calls"]))
                first_call, second_call = result["calls"]
                self.assertEqual(result["firstUser"], first_call[0])
                self.assertEqual(result["secondUser"], second_call[0])
                self.assertEqual(result["answer"], first_call[1])
                self.assertEqual(result["answer"], second_call[1])
                self.assertEqual(result["answer"], first_call[3])
                self.assertEqual(result["answer"], second_call[3])
                self.assertNotEqual(first_call[2], second_call[2])

    def test_generic_provider_keeps_opt_in_mutation_text_fallback(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(
                    browser, "public_generic_provider_mutation_fallback"
                )
                self.assertEqual(1, len(result["calls"]))
                self.assertEqual(result["answer"], result["calls"][0][1])
                self.assertEqual(result["answer"], result["calls"][0][3])

    def test_react_remount_with_same_sequence_is_not_a_new_assistant_turn(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "public_react_remount_baseline")
                self.assertEqual(1, result["callsWhileOnlyRemountExists"])
                self.assertEqual(2, len(result["calls"]))
                first_call, second_call = result["calls"]
                expected = result["expected"]
                self.assertEqual(expected["firstUser"], first_call[0])
                self.assertEqual(expected["priorAssistant"], first_call[1])
                self.assertEqual(expected["secondUser"], second_call[0])
                self.assertEqual(expected["secondAssistant"], second_call[1])
                self.assertEqual(expected["secondAssistant"], second_call[3])
                self.assertNotEqual(result["firstSession"], second_call[2])

    def test_old_delayed_pam_cleanup_never_mutates_the_newer_turn(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(
                    browser, "public_delayed_pam_cleanup_cross_turn"
                )
                self.assertEqual(2, result["callCount"])
                self.assertTrue(all(result["preservationChecks"]))
                first_call, second_call = result["calls"]
                expected = result["expected"]
                self.assertEqual(expected["firstUser"], first_call[0])
                self.assertEqual(expected["firstModel"], first_call[1])
                self.assertEqual(expected["firstDisplay"], first_call[3])
                self.assertEqual(expected["secondUser"], second_call[0])
                self.assertEqual(expected["secondModel"], second_call[1])
                self.assertEqual(expected["secondDisplay"], second_call[3])
                self.assertNotEqual(result["firstSession"], second_call[2])
                self.assertEqual(result["secondSession"], second_call[2])

    def test_rapid_next_turn_preserves_both_pending_debounce_stores(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(
                    browser, "public_rapid_next_turn_before_debounce"
                )
                self.assertEqual(0, result["callsBeforeSecondSubmit"])
                self.assertEqual(2, len(result["calls"]))
                first_call, second_call = result["calls"]
                expected = result["expected"]
                self.assertEqual(expected["firstUser"], first_call[0])
                self.assertEqual(expected["firstAssistant"], first_call[1])
                self.assertEqual(expected["firstAssistant"], first_call[3])
                self.assertEqual(expected["secondUser"], second_call[0])
                self.assertEqual(expected["secondAssistant"], second_call[1])
                self.assertEqual(expected["secondAssistant"], second_call[3])
                self.assertNotEqual(first_call[2], second_call[2])

    def test_actual_enriched_prompt_echo_is_never_stored_as_an_answer(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "public_enriched_prompt_echo")
                self.assertTrue(result["hasUserContext"])
                self.assertTrue(result["hasResponseFormat"])
                self.assertTrue(result["hasStPam"])
                self.assertTrue(result["hasMidPam"])
                self.assertTrue(result["hasEndPam"])
                self.assertEqual(0, result["callsAfterInitialDebounce"])
                self.assertEqual(0, result["callsAfterFallbackHorizon"])
                self.assertEqual([], result["calls"])

    def test_legitimate_complete_pam_answer_is_stored_once(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(
                    browser, "public_legitimate_complete_pam_answer"
                )
                self.assertEqual(1, len(result["calls"]))
                call = result["calls"][0]
                expected = result["expected"]
                self.assertEqual(expected["userSummary"], call[0])
                self.assertEqual(expected["modelSummary"], call[1])
                self.assertTrue(call[2])
                self.assertEqual(expected["userText"], result["retrieveCalls"][0][0])
                self.assertEqual(result["retrieveCalls"][0][1], call[2])
                self.assertEqual(expected["displayText"], call[3])

    def test_connection_error_with_request_diagnostics_is_not_stored(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(
                    browser, "public_reject_connection_error_with_diagnostics"
                )
                self.assertEqual(0, result["callsAfterInitialDebounce"])
                self.assertEqual(0, result["callsAfterFallbackHorizon"])
                self.assertEqual([], result["calls"])

    def test_exact_unknown_answers_are_never_stored(self) -> None:
        scenarios = (
            "public_reject_exact_unknown",
            "public_reject_exact_unknown_period",
            "public_reject_pam_unknown",
            "public_reject_pam_unknown_period",
        )
        for browser in BROWSERS:
            for scenario in scenarios:
                with self.subTest(browser=browser, scenario=scenario):
                    result = self.run_scenario(browser, scenario)
                    self.assertEqual(0, result["callsAfterInitialDebounce"])
                    self.assertEqual(0, result["callsAfterFallbackHorizon"])
                    self.assertEqual([], result["calls"])

    def test_legitimate_sentence_containing_unknown_is_stored_exactly(self) -> None:
        expected_answer = (
            "The status label Unknown means the source did not provide a recognized value."
        )
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_scenario(browser, "public_legitimate_unknown_prose")
                self.assertEqual(1, len(result["calls"]))
                call = result["calls"][0]
                self.assertEqual("Visible user prompt", call[0])
                self.assertEqual(expected_answer, call[1])
                self.assertTrue(call[2])
                self.assertEqual(expected_answer, call[3])
                self.assertEqual(result["retrieveCalls"][0][1], call[2])


if __name__ == "__main__":
    unittest.main()
