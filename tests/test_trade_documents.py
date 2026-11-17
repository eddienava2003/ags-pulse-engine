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
            bytes.fromhex("504B0304") + b"payload",
        ))

    def test_file_signature_validation_rejects_mismatches_and_binary_text(self):
        self.assertFalse(trade_documents._valid_file_signature("application/pdf", b"not a pdf"))
        self.assertFalse(trade_documents._valid_file_signature("text/plain", b"hello" + bytes([0]) + b"world"))

    def test_hs_code_format_check_distinguishes_format_from_classification(self):
        valid = trade_documents._hs_code_format_check(" 1234.56 ")
        self.assertTrue(valid["valid_format"])
        self.assertEqual(valid["normalized_code"], "123456")
        self.assertIn("not been verified", valid["message"].lower())
        self.assertFalse(trade_documents._hs_code_format_check("12AB56")["valid_format"])
        self.assertFalse(trade_documents._hs_code_format_check("12345")["valid_format"])

    def test_customs_routes_are_registered(self):
        paths = {route.path for route in trade_documents.router.routes}
        self.assertIn("/trade-documents/customs/hs-code/validate", paths)
        self.assertIn("/trade-documents/customs/hs-code/lookup", paths)
        self.assertIn("/trade-documents/customs/orders/{order_id}/readiness", paths)
        self.assertIn("/trade-documents/customs/orders/{order_id}/prepare", paths)

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



    def test_document_review_state_machine_helpers_are_registered(self):
        paths = {route.path for route in trade_documents.router.routes}
        self.assertIn("/trade-documents/{document_id}/decision", paths)
        self.assertIn("/trade-documents/{document_id}/upload", paths)
        self.assertIn("/trade-documents/{document_id}/render", paths)

    def test_human_decision_uses_compare_and_set_on_pending_review(self):
        class DecisionQuery:
            def __init__(self, returns):
                self.returns = returns
                self.filters = []
            def select(self, *_args, **_kwargs):
                return self
            def update(self, *_args, **_kwargs):
                return self
            def eq(self, key, value):
                self.filters.append((key, value))
                return self
            def order(self, *_args, **_kwargs):
                return self
            def limit(self, *_args, **_kwargs):
                return self
            def execute(self):
                return SimpleNamespace(data=self.returns)

        review_query = DecisionQuery([{"overall_risk": "LOW", "review_status": "COMPLETED"}])
        update_query = DecisionQuery([])
        class DecisionDatabase:
            def table(self, name):
                return review_query if name == "trade_document_reviews" else update_query

        with patch.object(trade_documents, "_auth_context", return_value=(DecisionDatabase(), "user-123", "org-123")), patch.object(
            trade_documents, "_document", return_value={"id": "doc-1", "status": "PENDING_REVIEW"}
        ):
            with self.assertRaises(trade_documents.HTTPException) as raised:
                trade_documents.decide_trade_document(
                    "doc-1", {"decision": "APPROVED"}, "Bearer token", "org-123"
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn(("status", "PENDING_REVIEW"), update_query.filters)

    def test_customs_packet_requires_certificate_for_cross_border_orders(self):
        required = trade_documents._required_customs_document_types({
            "buyer_country": "United States",
            "seller_country": "Mexico",
        })
        self.assertEqual(required, ["COMMERCIAL_INVOICE", "PACKING_LIST", "CERTIFICATE_OF_ORIGIN"])

    def test_customs_packet_omits_certificate_for_same_country_order(self):
        required = trade_documents._required_customs_document_types({
            "buyer_country": "United States",
            "seller_country": "united states",
        })
        self.assertEqual(required, ["COMMERCIAL_INVOICE", "PACKING_LIST"])

    def test_usitc_response_normalization(self):
        rows = trade_documents._usitc_search_records([
            {"htsno": "8541.43.00.10", "description": "Photovoltaic cells", "general": "Free", "special": "", "other": "35%"},
            {"unexpected": "shape"},
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["hts_number"], "8541.43.00.10")
        self.assertEqual(rows[0]["general_rate"], "Free")

    def test_usitc_lookup_returns_candidates_not_classification(self):
        fake_response = SimpleNamespace(
            headers={"content-type": "application/json"},
            text='[{"htsno":"8541.43.00.10","description":"Photovoltaic cells","general":"Free"}]',
            json=lambda: [{"htsno":"8541.43.00.10","description":"Photovoltaic cells","general":"Free"}],
            raise_for_status=lambda: None,
        )
        with patch.object(trade_documents, "_auth_context", return_value=(FakeDatabase(), "user-123", "org-123")), patch(
            "httpx.get", return_value=fake_response
        ) as request:
            result = trade_documents.lookup_us_customs_hs_code(
                {"hs_code":"8541430010","import_country":"United States"}
            )
        self.assertEqual(result["result_count"], 1)
        self.assertEqual(result["classification_status"], "CANDIDATES_RETRIEVED_NOT_CLASSIFIED")
        self.assertTrue(result["human_customs_review_required"])
        self.assertEqual(request.call_args.kwargs["params"]["keyword"], "8541430010")

    def test_usitc_lookup_rejects_non_us_market(self):
        with patch.object(trade_documents, "_auth_context", return_value=(FakeDatabase(), "user-123", "org-123")):
            with self.assertRaises(trade_documents.HTTPException) as raised:
                trade_documents.lookup_us_customs_hs_code(
                    {"product_description":"coffee beans","import_country":"Canada"}
                )
        self.assertEqual(raised.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
