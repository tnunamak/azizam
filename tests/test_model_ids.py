import unittest

from gateway.model_ids import display_model_id, is_path_like_model_id
from gateway.receipts import serialize_receipt_for_audience

GGUF = "/media/windows/AI Models/LLMs/GGUF/Qwen3.8-27B-Uncensored-Q4_K_M.gguf"


class ModelIdTests(unittest.TestCase):
    def test_paths_become_file_names_and_ids_stay(self) -> None:
        self.assertEqual(display_model_id(GGUF), "Qwen3.8-27B-Uncensored-Q4_K_M.gguf")
        self.assertEqual(display_model_id("C:\\Models\\llama.gguf"), "llama.gguf")
        for model_id in ("default", "openai/gpt-4o", "decide-quality", "tts-1"):
            self.assertFalse(is_path_like_model_id(model_id))
            self.assertEqual(display_model_id(model_id), model_id)

    def test_client_receipts_hide_host_paths_but_admin_and_internal_keep_them(self) -> None:
        receipt = {"request_id": "r1", "requested_model": "default", "resolved_model": GGUF}
        client = serialize_receipt_for_audience(receipt, "client")
        self.assertEqual(client["resolved_model"], "Qwen3.8-27B-Uncensored-Q4_K_M.gguf")
        self.assertEqual(client["requested_model"], "default")
        self.assertEqual(serialize_receipt_for_audience(receipt, "internal")["resolved_model"], GGUF)
        self.assertEqual(serialize_receipt_for_audience(receipt, "admin")["resolved_model"], GGUF)


if __name__ == "__main__":
    unittest.main()
