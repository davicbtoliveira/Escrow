from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from escrow.agreements.pii import (
    EncryptedValue,
    KmsEnvelopeCipher,
    PiiEncryptionUnavailable,
    envelope_cipher,
)


class EnvelopeEncryptionTests(SimpleTestCase):
    context = {
        "service": "escrow",
        "purpose": "customer-pii",
        "organization_id": "a4f584bc-0d7f-4637-a1a6-2bdb92a03e24",
        "agreement_id": "0a75a262-91ef-4c3e-b69b-5d95e085fb52",
        "version": "1",
    }

    @override_settings(
        PII_ENCRYPTION_BACKEND="kms",
        PII_KMS_KEY_ID="alias/escrow-local-application",
    )
    @patch("escrow.agreements.pii.boto3.client")
    def test_kms_generated_data_key_encrypts_one_aes_gcm_customer_blob(
        self,
        client_factory: MagicMock,
    ) -> None:
        kms = MagicMock()
        kms.generate_data_key.return_value = {
            "Plaintext": b"k" * 32,
            "CiphertextBlob": b"wrapped-data-key",
            "KeyId": "alias/escrow-local-application",
        }
        kms.decrypt.return_value = {"Plaintext": b"k" * 32}
        client_factory.return_value = kms
        plaintext = b'{"document":"52998224725","email":"ana@example.test","name":"Ana"}'

        cipher = KmsEnvelopeCipher()
        encrypted = cipher.encrypt(plaintext, self.context)
        decrypted = cipher.decrypt(encrypted, self.context)

        assert encrypted.ciphertext != plaintext
        assert encrypted.encrypted_data_key == b"wrapped-data-key"
        assert decrypted == plaintext
        kms.generate_data_key.assert_called_once_with(
            KeyId="alias/escrow-local-application",
            KeySpec="AES_256",
            EncryptionContext=self.context,
        )
        kms.decrypt.assert_called_once_with(
            CiphertextBlob=b"wrapped-data-key",
            KeyId="alias/escrow-local-application",
            EncryptionContext=self.context,
        )

    @override_settings(
        PII_ENCRYPTION_BACKEND="local",
        PII_LOCAL_ENCRYPTION_ALLOWED=False,
    )
    def test_local_cipher_fails_closed_when_not_in_development_or_tests(self) -> None:
        with self.assertRaises(PiiEncryptionUnavailable):
            envelope_cipher()

    @override_settings(PII_ENCRYPTION_BACKEND="local", PII_LOCAL_ENCRYPTION_ALLOWED=True)
    def test_tampered_aes_gcm_ciphertext_fails_closed(self) -> None:
        cipher = envelope_cipher()
        encrypted = cipher.encrypt(b"customer identity", self.context)
        tampered = EncryptedValue(
            ciphertext=encrypted.ciphertext[:-1] + b"x",
            nonce=encrypted.nonce,
            encrypted_data_key=encrypted.encrypted_data_key,
            kms_key_id=encrypted.kms_key_id,
        )

        with self.assertRaises(PiiEncryptionUnavailable):
            cipher.decrypt(tampered, self.context)
