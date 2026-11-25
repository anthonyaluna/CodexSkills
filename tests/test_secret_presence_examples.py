import re
import unittest
from pathlib import Path

REFERENCES = Path(__file__).resolve().parents[1] / "skills" / ".curated"


class SecretPresenceExamples(unittest.TestCase):
    def test_shell_diagnostics_only_expand_a_fixed_marker(self):
        examples = {
            "figma/references/figma-mcp-config.md": "FIGMA_OAUTH_TOKEN",
            "cloudflare-deploy/references/r2-sql/gotchas.md": "WRANGLER_R2_SQL_AUTH_TOKEN",
            "cloudflare-deploy/references/r2-sql/README.md": "WRANGLER_R2_SQL_AUTH_TOKEN",
        }
        for path, variable in examples.items():
            with self.subTest(path=path):
                content = (REFERENCES / path).read_text(encoding="utf-8")
                marker = "${" + variable + ":+set}"
                self.assertIn(f'test "{marker}" = set', content)
                self.assertNotRegex(content.replace(marker, ""), rf"\$\{{?{variable}\b")
                self.assertNotRegex(
                    content, rf"\b(?:printenv|declare -p)\s+{variable}\b"
                )

    def test_miniflare_logs_only_binding_presence(self):
        doc = REFERENCES / "cloudflare-deploy/references/miniflare/api.md"
        content = doc.read_text(encoding="utf-8")
        diagnostic = (
            'console.log("SECRET_KEY configured:", Boolean(bindings.SECRET_KEY));'
        )
        self.assertIn(diagnostic, content)
        other_logs = re.findall(
            r"console\.log\([^\n]+", content.replace(diagnostic, "")
        )
        self.assertFalse(any("SECRET_KEY" in statement for statement in other_logs))


if __name__ == "__main__":
    unittest.main()
