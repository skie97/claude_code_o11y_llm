"""
Tests for the pure project-attribution core (marker extraction + labelling).

The session-grouping and I/O are adapter concerns (describe_activity.py) and are
not exercised here. Run: cd scripts && python -m unittest test_project_attribution
"""

import unittest

from project_attribution import extract_markers, project_label


class ExtractMarkersTests(unittest.TestCase):
    def test_extracts_repo_name_from_windows_and_unix_paths(self):
        repos, _ = extract_markers(
            r"opened E:\Users\dev\Documents\GitHub\ExampleApp\firmware/main.cpp "
            r"and /home/dev/GitHub/claude_code_o11y_llm/scripts/analyze.py"
        )
        self.assertEqual(repos["ExampleApp"], 1)
        self.assertEqual(repos["claude_code_o11y_llm"], 1)

    def test_keeps_product_hosts_and_drops_infra_noise(self):
        _, hosts = extract_markers(
            "deployed to exampleapp.vercel.app; see api.github.com and example.com"
        )
        self.assertIn("exampleapp.vercel.app", hosts)
        self.assertNotIn("example.com", hosts)
        self.assertNotIn("api.github.com", hosts)  # github.com host is infra noise

    def test_ignores_too_short_repo_fragments(self):
        repos, _ = extract_markers("GitHub/ab and GitHub/xy")  # < 3 chars
        self.assertEqual(repos, {})


class ProjectLabelTests(unittest.TestCase):
    def test_prefers_repo_over_host(self):
        repos, hosts = extract_markers(
            "GitHub/ExampleApp GitHub/ExampleApp exampleapp.vercel.app"
        )
        self.assertEqual(project_label(repos, hosts), "ExampleApp")

    def test_falls_back_to_host_when_no_repo(self):
        repos, hosts = extract_markers("shipped at exampleapp.vercel.app")
        self.assertEqual(project_label(repos, hosts), "exampleapp.vercel.app")

    def test_unknown_when_no_signal(self):
        repos, hosts = extract_markers("i think it needs another 2px")
        self.assertEqual(project_label(repos, hosts), "unknown")


if __name__ == "__main__":
    unittest.main()
