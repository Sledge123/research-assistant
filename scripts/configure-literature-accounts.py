"""安全导入科研通账号；只打印账号数量，不输出秘密。"""
import argparse
import getpass
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from highscore_literature.credentials import load_accounts,save_accounts


def vault_password(gui):
    if not gui:
        return getpass.getpass('请在本机输入来源账号库主密码（不会显示）：')
    import tkinter as tk
    from tkinter import simpledialog
    window=tk.Tk()
    window.withdraw()
    window.attributes('-topmost',True)
    try:
        value=simpledialog.askstring('晶谱 · 安全迁移科研通账号',
            '请在本机输入原科研通账号库主密码。\n仅用于本机解密并迁入项目加密配置，不发送到聊天或日志。',
            show='*',parent=window)
    finally:
        window.destroy()
    if value is None:raise ValueError('用户未完成本机安全输入。')
    return value


def main():
    parser=argparse.ArgumentParser(description='配置项目内加密科研通账号')
    parser.add_argument('--import-env',type=Path,help='从已有工程.env安全导入，不写回来源')
    parser.add_argument('--import-vault',type=Path,help='从既有账号库导入，主密码在本地安全输入')
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--merge',action='store_true',help='将导入账号合并到已有加密配置，同用户名只保留一份')
    parser.add_argument('--gui',action='store_true',help='通过本机隐藏字符输入框输入账号库主密码')
    args=parser.parse_args()
    if args.check:
        print(f'PASS 文献账号加密配置可读，启用账号数 {len(load_accounts(ROOT))}')
        return 0
    accounts=[]
    if args.import_env:
        from dotenv import dotenv_values
        source=dotenv_values(args.import_env)
        accounts.append({'username':source.get('ABLESCI_USERNAME'),'password':source.get('ABLESCI_PASSWORD'),'enabled':True})
    if args.import_vault:
        import base64
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        envelope=json.loads(args.import_vault.read_text(encoding='utf-8'))
        if envelope.get('format')!='ablesci-account-vault' or envelope.get('version')!=1:
            raise ValueError('来源账号库格式不受支持。')
        master=vault_password(args.gui)
        derived=Scrypt(salt=base64.b64decode(envelope['kdf']['salt_b64']),length=32,n=32768,r=8,p=1).derive(master.encode())
        payload=AESGCM(derived).decrypt(base64.b64decode(envelope['cipher']['nonce_b64']),base64.b64decode(envelope['ciphertext_b64']),b'ablesci-account-vault-v1')
        accounts.extend(json.loads(payload.decode('utf-8'))['accounts'])
    if not accounts:
        accounts=[{'username':input('科研通账号：').strip(),'password':getpass.getpass('科研通密码（不显示）：'),'enabled':True}]
    if args.merge and (ROOT/'runtime/literature-credentials/accounts.encrypted.json').exists():
        accounts.extend(load_accounts(ROOT))
    count=save_accounts(ROOT,accounts)
    assert len(load_accounts(ROOT))<=count
    print(f'PASS 已安全保存并读回文献账号配置，账号数 {count}；来源文件未修改。')
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:
        print('FAIL 账号导入未完成，请检查来源解锁材料和项目内私有配置；未输出秘密。',file=sys.stderr)
        raise SystemExit(1)
