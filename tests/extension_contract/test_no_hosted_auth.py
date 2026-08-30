"""The browser integration and local daemon expose no biomem account system."""

from __future__ import annotations

from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
BROWSER_ROOTS = {
    name: REPO_ROOT / "extensions" / f"{name}-src"
    for name in ("chrome", "firefox", "safari")
}
TEXT_SUFFIXES = {".html", ".js", ".json", ".md", ".css"}
FORBIDDEN_EXTENSION_TERMS = (
    "tokenEndpoint",
    "apiToken",
    "localMode",
    "fetchToken",
    "getToken",
    "tokenCache",
    "api_key",
    "AUTH_REQUIRED",
    "Missing JWT token",
    "hosted service",
    "auth service",
    "Username (email)",
    "Token Endpoint",
    "biomem key",
    "registration",
    "registering",
)


class NoHostedAuthContractTests(unittest.TestCase):
    def test_browser_sources_expose_no_hosted_auth_surface(self) -> None:
        for browser, root in BROWSER_ROOTS.items():
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
                    continue
                source = path.read_text(encoding="utf-8")
                for term in FORBIDDEN_EXTENSION_TERMS:
                    with self.subTest(browser=browser, path=path.name, term=term):
                        self.assertNotIn(term, source)

    def test_local_backend_has_no_biomem_token_authentication(self) -> None:
        module_root = REPO_ROOT / "src" / "memory_module"
        self.assertFalse((module_root / "token_validator.py").exists())

        forbidden_by_file = {
            module_root / "security.py": (
                "key_file",
                "store_api_key",
                "load_api_key",
                "has_api_key",
                "delete_api_key",
                "verify_api_key",
            ),
            module_root / "config.py": ("bdbm_key.enc", "key_file"),
            module_root / "ws_server.py": ("key_file",),
            module_root / "protocol.py": ("Verify API key", "INVALID_KEY"),
        }
        for path, terms in forbidden_by_file.items():
            source = path.read_text(encoding="utf-8")
            for term in terms:
                with self.subTest(path=path.name, term=term):
                    self.assertNotIn(term, source)

    def test_desktop_byok_provider_keys_remain_separate(self) -> None:
        source = (REPO_ROOT / "src" / "memory_module" / "llm_client.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("def has_api_key", source)
        self.assertIn("NO_API_KEY", source)


if __name__ == "__main__":
    unittest.main()
