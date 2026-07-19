"""固件签名验证 API 路由

提供固件哈希校验、签名验证、清单生成能力。
- 哈希算法使用 hashlib
- 签名验证使用 hmac（无可用的非对称加密库时返回 503）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from edgelite.api.deps import require_permission
from edgelite.models.common import ApiResponse
from edgelite.security.rbac import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/firmware", tags=["Firmware Signature"])

_SUPPORTED_HASH_ALGORITHMS = {
    "md5",
    "sha1",
    "sha224",
    "sha256",
    "sha384",
    "sha512",
    "sha3_256",
    "sha3_512",
}

_DEFAULT_CHUNK_SIZE = 64 * 1024


class SignatureVerifyRequest(BaseModel):
    firmware_path: str = Field(..., min_length=1, max_length=512)
    signature: str = Field(..., min_length=1)
    public_key: str | None = Field(default=None, max_length=8192)


class HashVerifyRequest(BaseModel):
    firmware_path: str = Field(..., min_length=1, max_length=512)
    algorithm: str = Field(default="sha256", max_length=32)
    expected_hash: str = Field(..., min_length=8, max_length=256)


class ManifestGenerateRequest(BaseModel):
    firmware_path: str = Field(..., min_length=1, max_length=512)
    algorithm: str | None = Field(default="sha256", max_length=32)


def _compute_file_hash(path: str, algorithm: str) -> str:
    algo = algorithm.lower().replace("-", "_")
    if algo not in _SUPPORTED_HASH_ALGORITHMS:
        raise ValueError(f"unsupported algorithm: {algorithm}")
    hasher = hashlib.new(algo)
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_DEFAULT_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _file_size(path: str) -> int:
    return os.path.getsize(path)


@router.post("/verify/signature", response_model=ApiResponse)
async def verify_signature(
    req: SignatureVerifyRequest,
    user: dict[str, str] = Depends(require_permission(Permission.OTA_MANAGE)),
):
    """验证固件签名

    本端点使用 hmac + sha256 做对称签名校验。
    若需非对称验签（如 RSA/Ed25519），且环境未安装 cryptography，
    则返回 503 ERR_COMMON_SERVICE_NOT_READY。
    """
    try:
        if not os.path.isfile(req.firmware_path):
            raise HTTPException(status_code=404, detail="ERR_COMMON_NOT_FOUND")

        # 优先尝试非对称验签（若 cryptography 可用且提供 public_key）
        if req.public_key:
            try:
                from cryptography.hazmat.primitives import hashes, serialization
                from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
            except ImportError:
                raise HTTPException(
                    status_code=503,
                    detail="ERR_COMMON_SERVICE_NOT_READY",
                ) from None

            try:
                pub_pem = req.public_key.encode("utf-8")
                public_key = serialization.load_pem_public_key(pub_pem)
                with open(req.firmware_path, "rb") as fh:
                    data = fh.read()
                try:
                    sig_bytes = base64.b64decode(req.signature)
                except Exception as exc:
                    raise HTTPException(
                        status_code=400, detail="ERR_COMMON_VALIDATION_FAILED"
                    ) from exc

                if isinstance(public_key, rsa.RSAPublicKey):
                    public_key.verify(
                        sig_bytes,
                        data,
                        padding.PKCS1v15(),
                        hashes.SHA256(),
                    )
                    verified = True
                elif isinstance(public_key, ec.EllipticCurvePublicKey):
                    public_key.verify(sig_bytes, data, ec.ECDSA(hashes.SHA256()))
                    verified = True
                else:
                    raise HTTPException(
                        status_code=400, detail="ERR_COMMON_VALIDATION_FAILED"
                    ) from None
                return ApiResponse(
                    data={
                        "verified": verified,
                        "algorithm": "asymmetric",
                        "firmware_path": req.firmware_path,
                        "verified_at": datetime.now(UTC).isoformat(),
                    }
                )
            except HTTPException:
                raise
            except Exception as e:
                logger.error("firmware asymmetric verify failed: %s", e)
                return ApiResponse(
                    data={
                        "verified": False,
                        "algorithm": "asymmetric",
                        "error": str(e),
                        "verified_at": datetime.now(UTC).isoformat(),
                    }
                )

        # 无 public_key 时使用 hmac-symmetric 验签（共享密钥从环境变量读取）
        secret = os.environ.get("EDGELITE_FIRMWARE_SECRET", "").encode("utf-8")
        if not secret:
            raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None

        with open(req.firmware_path, "rb") as fh:
            data = fh.read()
        expected = hmac.new(secret, data, hashlib.sha256).hexdigest()
        try:
            sig_bytes = base64.b64decode(req.signature)
            sig_hex = sig_bytes.hex()
        except Exception:
            sig_hex = req.signature.strip().lower()

        verified = hmac.compare_digest(expected, sig_hex.lower())
        return ApiResponse(
            data={
                "verified": verified,
                "algorithm": "hmac-sha256",
                "firmware_path": req.firmware_path,
                "verified_at": datetime.now(UTC).isoformat(),
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("firmware verify signature failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/verify/hash", response_model=ApiResponse)
async def verify_hash(
    req: HashVerifyRequest,
    user: dict[str, str] = Depends(require_permission(Permission.OTA_MANAGE)),
):
    """验证固件哈希"""
    try:
        if not os.path.isfile(req.firmware_path):
            raise HTTPException(status_code=404, detail="ERR_COMMON_NOT_FOUND")
        try:
            actual = _compute_file_hash(req.firmware_path, req.algorithm)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="ERR_COMMON_VALIDATION_FAILED") from exc
        verified = hmac.compare_digest(actual.lower(), req.expected_hash.strip().lower())
        return ApiResponse(
            data={
                "verified": verified,
                "algorithm": req.algorithm.lower(),
                "firmware_path": req.firmware_path,
                "expected_hash": req.expected_hash,
                "actual_hash": actual,
                "verified_at": datetime.now(UTC).isoformat(),
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("firmware verify hash failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None


@router.post("/manifest/generate", response_model=ApiResponse)
async def generate_manifest(
    req: ManifestGenerateRequest,
    user: dict[str, str] = Depends(require_permission(Permission.OTA_MANAGE)),
):
    """生成固件清单（路径、大小、多算法哈希、生成时间）"""
    try:
        if not os.path.isfile(req.firmware_path):
            raise HTTPException(status_code=404, detail="ERR_COMMON_NOT_FOUND")
        algorithm = (req.algorithm or "sha256").lower()
        try:
            primary_hash = _compute_file_hash(req.firmware_path, algorithm)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="ERR_COMMON_VALIDATION_FAILED") from exc

        # 附加 sha256 作为通用 fallback
        sha256_hash = (
            primary_hash
            if algorithm == "sha256"
            else _compute_file_hash(req.firmware_path, "sha256")
        )
        manifest: dict[str, Any] = {
            "firmware_path": req.firmware_path,
            "size_bytes": _file_size(req.firmware_path),
            "algorithm": algorithm,
            "hash": primary_hash,
            "sha256": sha256_hash,
            "generated_at": datetime.now(UTC).isoformat(),
        }
        return ApiResponse(data=manifest)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("firmware manifest generate failed: %s", e)
        raise HTTPException(status_code=503, detail="ERR_COMMON_SERVICE_NOT_READY") from None
