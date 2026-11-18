"""Công cụ tự động kiểm chứng đọc email & OTP thực tế từ tài khoản Hotmail/Outlook.

Cách sử dụng trên máy của bạn (PowerShell hoặc Terminal):
    python run_live_test.py

Công cụ sẽ yêu cầu nhập chuỗi định dạng (hoặc tự động đọc từ file account.txt nếu có).
Hỗ trợ cả chuỗi 4 trường chuẩn và chuỗi mở rộng (> 4 trường):
    email|password|refresh_token|client_id[|extra_fields...]
"""

import os
import sys
from datetime import datetime, timedelta, timezone

from hotmail_otp_sdk import (
    ConfigOptions,
    HotmailAccount,
    HotmailOtpError,
    OtpOptions,
    OtpQuery,
    OtpReader,
)
from hotmail_otp_sdk._redact import mask_email


def parse_raw_account(line: str) -> HotmailAccount:
    """Tách và trích xuất đúng 4 trường đầu tiên từ chuỗi người dùng."""
    parts = [p.strip() for p in line.strip().split("|")]
    if len(parts) < 4:
        raise ValueError(
            f"Chuỗi không đủ 4 trường cơ bản (yêu cầu ít nhất 4 trường, nhận được {len(parts)} trường)."
        )

    email, password, refresh_token, client_id = parts[:4]
    if not email or not password or not refresh_token or not client_id:
        raise ValueError("Các trường email, password, refresh_token, client_id không được để trống.")

    if len(parts) > 4:
        extra_count = len(parts) - 4
        print(f"[*] Phát hiện {len(parts)} trường. Tự động lấy 4 trường chuẩn và bỏ qua {extra_count} trường phụ.")

    return HotmailAccount(
        email=email,
        password=password,
        refresh_token=refresh_token,
        client_id=client_id,
    )


def run_verification(raw_line: str) -> None:
    try:
        account = parse_raw_account(raw_line)
    except ValueError as exc:
        print(f"[!] Lỗi phân tích chuỗi tài khoản: {exc}", file=sys.stderr)
        return

    masked = mask_email(account.email)
    print(f"\n[+] Bắt đầu kiểm chứng với tài khoản: {masked}")
    print("[+] Đang kết nối Microsoft OAuth2 để xác thực và làm mới token...")

    config = ConfigOptions(
        provider="GRAPH",
        fallback_to_imap=True,
    )

    try:
        with OtpReader(account, config) as reader:
            print("[+] Xác thực thành công!")
            print("[+] Đang quét 5 email mới nhất (Inbox & Junk)...")

            messages = reader.get_latest_messages(top=5, folder="both")
            if not messages:
                print(f"[-] Hộp thư {masked} hiện không có thư nào.")
            else:
                print(f"[V] Đã đọc thành công {len(messages)} email mới nhất:\n")
                print("-" * 75)
                for idx, msg in enumerate(messages, 1):
                    received_str = msg.received_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                    print(f"#{idx} | Thời gian: {received_str}")
                    print(f"   Người gửi: {msg.sender}")
                    print(f"   Tiêu đề  : {msg.subject}")
                    if msg.body_text:
                        preview = msg.body_text[:120].replace("\n", " ").strip()
                        print(f"   Trích đoạn: {preview}...")
                    print("-" * 75)

            # Thử nghiệm bóc tách OTP nếu có yêu cầu
            print("\n[+] Đang kiểm tra mã OTP gần đây (trong vòng 60 phút qua)...")
            otp_result = reader.wait_for_otp(
                OtpQuery(
                    since=datetime.now(timezone.utc) - timedelta(minutes=60),
                    folder="both",
                ),
                OtpOptions(timeout_ms=5_000, poll_interval_ms=1_000),
            )
            if otp_result.status == "SUCCESS" and otp_result.code:
                print(f"[V] Tìm thấy mã OTP: >>> {otp_result.code} <<<")
                print(f"    Từ: {otp_result.sender} | Tiêu đề: {otp_result.subject}")
            elif otp_result.status == "TIMEOUT":
                print("[-] Không có mã OTP nào trong vòng 60 phút qua.")
            else:
                print(f"[-] Trạng thái OTP: {otp_result.status} (Chi tiết: {otp_result.error or 'Không có'})")

    except HotmailOtpError as exc:
        print(f"\n[X] Lỗi từ Microsoft Service [{exc.code}]: {exc}", file=sys.stderr)
    except OSError as exc:
        print(f"\n[X] Lỗi kết nối mạng: {exc}", file=sys.stderr)


def main() -> None:
    print("=" * 75)
    print(" CÔNG CỤ TỰ ĐỘNG KIỂM CHỨNG ĐỌC MAIL & OTP HOTMAIL / OUTLOOK")
    print("=" * 75)

    account_line = os.environ.get("HOTMAIL_OTP_ACCOUNT", "").strip()
    if not account_line and os.path.exists("account.txt"):
        try:
            with open("account.txt", encoding="utf-8") as f:
                account_line = f.readline().strip()
            if account_line:
                print("[*] Đã tự động đọc tài khoản từ file account.txt.")
        except OSError:
            pass

    if not account_line:
        print("\nNhập chuỗi tài khoản của bạn (định dạng: email|password|refresh_token|client_id):")
        try:
            account_line = input(">> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nĐã hủy.")
            return

    if not account_line:
        print("[!] Không có dữ liệu đầu vào. Đã dừng.")
        return

    run_verification(account_line)


if __name__ == "__main__":
    main()
