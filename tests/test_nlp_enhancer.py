import unittest

from src.intelligence.nlp_enhancer import NLPEnhancer


class NLPEnhancerTests(unittest.TestCase):
    def setUp(self):
        self.enhancer = NLPEnhancer()

    def test_translates_top_cpu_process_with_path_and_permissions(self):
        result = self.enhancer.translate_to_command(
            "帮我查看当前系统中CPU占用最高的程序，并指出该程序的路径以及相关的权限"
        )

        self.assertGreater(result.confidence, 0.9)
        self.assertIn("readlink -f /proc/$pid/exe", result.translated_command)
        self.assertIn("awk 'NR==1{print $1}'", result.translated_command)
        self.assertIn("stat -c", result.translated_command)
        self.assertIn("${exe:-N/A}", result.translated_command)
        self.assertNotEqual(result.translated_command, "ls .")

    def test_still_lists_directory_contents(self):
        result = self.enhancer.translate_to_command("查看 /tmp 目录内容")

        self.assertEqual(result.translated_command, "ls /tmp")


if __name__ == "__main__":
    unittest.main()
