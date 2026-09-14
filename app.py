# app.py
import io
import asyncio
import csv
import json
from typing import List, Dict, Any

import logging
from logging.handlers import RotatingFileHandler

import pandas as pd
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from api_solana_client import ApiSolanaClient, Pubkey, get_associated_token_address, create_associated_token_account
from solders.system_program import transfer as transfer_sol, TransferParams as TransferParamsSol
from spl.token.instructions import transfer as spl_transfer, TransferParams
from spl.token.constants import TOKEN_PROGRAM_ID
from solana.transaction import Transaction
from solana.rpc.commitment import Finalized

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
# templates = Jinja2Templates(directory="templates")  # Если нужно рендерить шаблоны, раскомментировать

# Хранилища в памяти
_df_wallets: pd.DataFrame = pd.DataFrame(columns=["public_key", "private_key"])
_tokens: List[Dict[str, Any]] = []  # список токенов {name, mint}

# Глобальный список для логов
LOGS = []

def get_logger(logfile: str = "SOLANA_WALLET.log") -> logging.Logger:
    logger = logging.getLogger("SolanaWalletLogger")
    logger.setLevel(logging.INFO)

    if logger.hasHandlers():
        logger.handlers.clear()

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    formatter_console = logging.Formatter('[%(asctime)s] %(message)s', datefmt="%Y-%m-%d %H:%M:%S")
    ch.setFormatter(formatter_console)
    logger.addHandler(ch)

    # File handler
    fh = RotatingFileHandler(logfile, maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
    fh.setLevel(logging.INFO)
    formatter_file = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', datefmt="%Y-%m-%d %H:%M:%S")
    fh.setFormatter(formatter_file)
    logger.addHandler(fh)

    # Memory handler для логов в UI
    class MemoryHandler(logging.Handler):
        def emit(self, record):
            msg = self.format(record)
            LOGS.append(msg)
            if len(LOGS) > 1000:
                LOGS.pop(0)
    mh = MemoryHandler()
    mh.setLevel(logging.INFO)
    formatter_mem = logging.Formatter('[%(asctime)s] %(message)s', datefmt="%Y-%m-%d %H:%M:%S")
    mh.setFormatter(formatter_mem)
    logger.addHandler(mh)

    return logger

log = get_logger()

# HTML интерфейс
INDEX_HTML = ""

with open("index.html","r") as f:
    INDEX_HTML = f.read()

@app.get("/", response_class=HTMLResponse)
async def root():
    return INDEX_HTML

@app.get("/logs")
async def get_logs():
    return JSONResponse(LOGS)

@app.post("/upload_wallets")
async def upload_wallets(file: UploadFile = File(...)):
    global _df_wallets
    content = await file.read()
    s = content.decode('utf-8')
    df = pd.read_csv(io.StringIO(s))
    required_cols = ['public_key', 'private_key']
    if not all(col in df.columns for col in required_cols):
        return JSONResponse({"detail": "CSV должен содержать: public_key, private_key"}, status_code=400)
    _df_wallets = df[['public_key', 'private_key']].copy()
    log.info(f"Загружено {_df_wallets.shape[0]} кошельков")
    return {"detail": f"OK, {_df_wallets.shape[0]} wallets loaded"}

@app.post("/upload_tokens")
async def upload_tokens(file: UploadFile = File(...)):
    global _tokens
    content = await file.read()
    s = content.decode('utf-8')
    df = pd.read_csv(io.StringIO(s))
    required_cols = ['name', 'mint']
    if not all(col in df.columns for col in required_cols):
        return JSONResponse({"detail": "CSV должен содержать: name, mint"}, status_code=400)
    _tokens = df.to_dict(orient='records')
    log.info(f"Загружено {len(_tokens)} токенов")
    return {"detail": f"OK, {len(_tokens)} tokens loaded"}

@app.get("/tokens")
async def get_tokens():
    return JSONResponse(_tokens)

@app.get("/wallets/full_status")
async def get_wallets_status():
    global _df_wallets, _tokens
    if _df_wallets.empty:
        return JSONResponse([])
    res = []
    for i, row in _df_wallets.iterrows():
        wallet_pub = row['public_key']
        wallet_priv = row['private_key']
        client = ApiSolanaClient(log, wallet_priv, "So11111111111111111111111111111111111111112")  # Dummy mint for init
        wallet_info = {"index": i, "public_key": wallet_pub, "balances": {}}
        try:
            sol_balance = await client.get_balance(Pubkey.from_string(wallet_pub))
            wallet_info["balances"]["SOL"] = sol_balance / 1e9
        except:
            wallet_info["balances"]["SOL"] = None
        try:
            ata_usdc = get_associated_token_address(Pubkey.from_string(wallet_pub), Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"))
            open_usdc = await client.is_token_account_open(Pubkey.from_string(wallet_pub), Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"))
            bal_usdc_raw = 0
            if open_usdc:
                bal_usdc_raw = await client.get_token_amount_raw(ata_usdc)
            dec = 6  # USDC decimals
            wallet_info["balances"]["USDC"] = bal_usdc_raw / (10**dec)
            wallet_info["ATA_USDC_open"] = open_usdc
        except:
            wallet_info["balances"]["USDC"] = None
            wallet_info["ATA_USDC_open"] = False
        for t in _tokens:
            mint = t["mint"]
            try:
                is_open = await client.is_token_account_open(Pubkey.from_string(wallet_pub), Pubkey.from_string(mint))
                bal_raw = 0
                dec = 0
                if is_open:
                    ata = get_associated_token_address(Pubkey.from_string(wallet_pub), Pubkey.from_string(mint))
                    bal_raw = await client.get_token_amount_raw(ata)
                    dec = await client.get_token_decimals(Pubkey.from_string(mint))
                wallet_info["balances"][t.get("name", mint)] = bal_raw / (10**dec)
                wallet_info[f"ATA_{t.get('name', mint)}_open"] = is_open
            except:
                wallet_info["balances"][t.get("name", mint)] = None
                wallet_info[f"ATA_{t.get('name', mint)}_open"] = False
        res.append(wallet_info)
    return JSONResponse(res)

@app.post("/swap")
async def perform_swap(request: Request):
    payload = await request.json()
    wallet_index = int(payload['wallet_index'])
    input_mint = payload['input_mint']
    output_mint = payload['output_mint']
    amount_ui = float(payload['amount_ui'])
    slippage_bps = int(payload.get('slippage_bps', 50))
    row = _df_wallets.iloc[wallet_index]
    client = ApiSolanaClient(log, row['private_key'], input_mint)
    sig = await client.swap_tokens(input_mint, output_mint, amount_ui, slippage_bps, amount_is_input=True)
    if sig:
        log.info(f"Swap successful: {sig}")
        return {"signature": sig}
    else:
        return JSONResponse({"detail": "Swap failed"}, status_code=500)

@app.post("/transfer")
async def perform_transfer(request: Request):
    payload = await request.json()
    wallet_index = int(payload['wallet_index'])
    token_mint = payload['token_mint']  # "SOL" or mint
    recipient = payload['recipient']
    amount_ui = float(payload['amount_ui'])
    row = _df_wallets.iloc[wallet_index]
    client = ApiSolanaClient(log, row['private_key'], token_mint if token_mint != "SOL" else "So11111111111111111111111111111111111111112")
    sender_pub = Pubkey.from_string(row['public_key'])
    recipient_pub = Pubkey.from_string(recipient)

    # Check balance
    if token_mint == "SOL":
        balance = await client.get_balance(sender_pub) / 1e9
        if balance < amount_ui:
            return JSONResponse({"detail": "Insufficient SOL balance"}, status_code=400)
        sig = await client.transfer_sol(recipient_pub, int(amount_ui * 1e9))
    else:
        ata_sender = get_associated_token_address(sender_pub, Pubkey.from_string(token_mint))
        balance_raw = await client.get_token_amount_raw(ata_sender)
        dec = await client.get_token_decimals(Pubkey.from_string(token_mint))
        balance_ui = balance_raw / (10**dec)
        if balance_ui < amount_ui:
            return JSONResponse({"detail": "Insufficient token balance"}, status_code=400)
        ata_recipient = get_associated_token_address(recipient_pub, Pubkey.from_string(token_mint))
        if not await client.is_token_account_open(recipient_pub, Pubkey.from_string(token_mint)):
            tx_create = Transaction(fee_payer=sender_pub).add(
                create_associated_token_account(sender_pub, recipient_pub, Pubkey.from_string(token_mint))
            )
            await client._send_and_confirm(tx_create)
        tx = Transaction(fee_payer=sender_pub).add(
            spl_transfer(TransferParams(
                program_id=TOKEN_PROGRAM_ID,
                source=ata_sender,
                dest=ata_recipient,
                owner=sender_pub,
                amount=int(amount_ui * (10**dec))
            ))
        )
        sig = await client._send_and_confirm(tx)
    if sig:
        log.info(f"Transfer successful: {sig}")
        return {"signature": sig}
    else:
        return JSONResponse({"detail": "Transfer failed"}, status_code=500)

@app.post("/close_ata")
async def close_ata(request: Request):
    payload = await request.json()
    wallet_index = int(payload['wallet_index'])
    token_mint = payload['token_mint']
    row = _df_wallets.iloc[wallet_index]
    client = ApiSolanaClient(log, row['private_key'], token_mint)
    sender_pub = Pubkey.from_string(row['public_key'])
    ata = get_associated_token_address(sender_pub, Pubkey.from_string(token_mint))
    balance_raw = await client.get_token_amount_raw(ata)
    if balance_raw > 0:
        return JSONResponse({"detail": "ATA not empty"}, status_code=400)
    sig = await client.close_ata_account(ata, row['public_key'])
    if sig:
        log.info(f"ATA closed: {sig}")
        return {"signature": sig}
    else:
        return JSONResponse({"detail": "Close failed"}, status_code=500)