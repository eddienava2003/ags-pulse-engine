import unittest
from types import SimpleNamespace
from unittest.mock import patch

import trade_documents


class FakeQuery:
    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def execute(self):
        return SimpleNamespace(data=[{"organization_id": "org-123"}])


class FakeAuth:
    def get_user(self, _token):
        return SimpleNamespace(user=SimpleNamespace(id="user-123"))


class FakeDatabase:
    auth = FakeAuth()

    def table(self, _name):
        return FakeQuery()


class TradeDocumentBackendTests(unittest.TestCase):
    def test_filename_is_sanitized(self):
        self.assertEqual(
            trade_documents._safe_filename("../../invoice:2026", "P.D.F"),
            "invoice-2026.pdf",
        )

    def test_supported_document_types_include_customs_documents(self):
        for doc_type in ("COMMERCIAL_INVOICE", "PACKING_LIST", "BOL_DRAFT", "CERTIFICATE_OF_ORIGIN"):
            self.assertIn(doc_type, trade_documents.DOCUMENT_TYPES)

    def test_file_signature_validation_accepts_real_pdf_and_docx_headers(self):
        self.assertTrue(trade_documents._valid_file_signature("application/pdf", b"%PDF-1.7"))
        self.assertTrue(trade_documents._valid_file_signature(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            b"PK\\x03\\x04payload",
        ))

    def test_file_signature_validation_rejects_mismatches_and_binary_text(self):
        self.assertFalse(trade_documents._valid_file_signature("application/pdf", b"not a pdf"))
        self.assertFalse(trade_documents._valid_file_signature("text/plain", b"hello\\x00world"))

    def test_api_routes_are_registered(self):
        paths = {route.path for route in trade_documents.router.routes}
        self.assertIn("/trade-documents", paths)
        self.assertIn("/trade-documents/draft", paths)
        self.assertIn("/trade-documents/{document_id}/review", paths)
        self.assertIn("/trade-documents/{document_id}/decision", paths)
        self.assertIn("/trade-documents/{document_id}/render", paths)
        self.assertIn("/trade-documents/{document_id}/upload", paths)
        self.assertIn("/trade-documents/{document_id}/download", paths)

    def test_auth_context_resolves_verified_org_membership(self):
        with patch.object(trade_documents, "_client", return_value=FakeDatabase()):
            _db, user_id, org_id = trade_documents._auth_context("Bearer test-token")
        self.assertEqual(user_id, "user-123")
        self.assertEqual(org_id, "org-123")

    def test_auth_context_rejects_missing_bearer_token(self):
        with self.assertRaises(trade_documents.HTTPException) as raised:
            trade_documents._auth_context("")
        self.assertEqual(raised.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
