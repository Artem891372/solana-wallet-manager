from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional, Dict, Any

import base58
import requests
import base64
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.message import to_bytes_versioned, MessageV0
from solders.transaction import VersionedTransaction
from solders.transaction_status import TransactionConfirmationStatus

from solana.transaction import Transaction
from solana.rpc.types import TxOpts, TokenAccountOpts
from solana.rpc.commitment import Confirmed, Finalized, Processed, Commitment

from solana_proxy import AsyncClient  

from solders.system_program import TransferParams as TransferParamsSol
from solders.system_program import transfer as transfer_sol

from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.async_client import AsyncToken
from spl.token.instructions import (
    transfer as spl_transfer,
    TransferParams,
    create_associated_token_account,
    get_associated_token_address,
    close_account,
    CloseAccountParams,
)

from config.config_load import load_config_json

config = load_config_json(module="ApiSolanaClient")

MIN_SOL_RESERVE: int = int(config["MIN_SOL_RESERVE"])
MAX_TIMEOUT: int = int(config["MAX_TIMEOUT"])
RETRY_ATTEMPTS: int = int(config["RETRY_ATTEMPTS"])
RETRY_DELAY: float = float(config["RETRY_DELAY"])
RPC = config["RPC"]
DEFAULT_COMMITMENT: Commitment = Confirmed  # баланс между скоростью и надёжностью
JUPITER_QUOTE_URL = config.get("JUPITER_QUOTE_URL", "https://public.jupiterapi.com/quote")
JUPITER_SWAP_URL = config.get("JUPITER_SWAP_URL", "https://public.jupiterapi.com/swap")
WSOL_MINT = "So11111111111111111111111111111111111111112"


class ApiSolanaClient:
    """Клиент для взаимодействия с блокчейном Solana. Улучшен с учётом лучших практик из wallet_interface.py."""

    def __init__(self, logger: logging.Logger, wallet_secret_base58: str, token_mint_address: str):
        self.logger = logger
        # Важно указывать commitment и timeout на клиенте
        self.client = AsyncClient(RPC,  timeout=60, commitment=DEFAULT_COMMITMENT) #proxy=TOR_PROXY,
        # fallback_client на случай проблем с основным RPC (как в wallet_interface)
        self.fallback_client = AsyncClient(RPC, timeout=60, commitment=DEFAULT_COMMITMENT) #, proxy=TOR_PROXY  # Можно заменить на другой RPC если нужно
        # ожидаем base58 от 64-байтного секретного ключа (ed25519)
        self.sender_keypair = Keypair.from_bytes(base58.b58decode(wallet_secret_base58))
        self.sender_pubkey = self.sender_keypair.pubkey()
        self.token_mint_pubkey = Pubkey.from_string(token_mint_address)

    # ---------- Улучшенные низкоуровневые утилиты (ретраи с backoff, fallback) ----------

    async def _latest_blockhash(self, use_fallback: bool = False) -> tuple:
        """Получить актуальный blockhash + lastValidBlockHeight с ретраями и fallback."""
        client = self.fallback_client if use_fallback else self.client
        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = await client.get_latest_blockhash()
                value = resp.value
                return value.blockhash, value.last_valid_block_height
            except Exception as e:
                self.logger.warning("_latest_blockhash attempt %d failed: %s", attempt + 1, e)
                await asyncio.sleep(RETRY_DELAY * (2 ** attempt))  # Экспоненциальный backoff
        raise RuntimeError("Failed to fetch latest blockhash after retries")

    async def _send_and_confirm(self, tx: Transaction, *, preflight: bool = True, commitment: Commitment = Finalized, use_fallback: bool = False) -> Optional[str]:
        """Надёжная отправка с актуальным blockhash, ретраями, backoff и fallback."""
        client = self.fallback_client if use_fallback else self.client
        for attempt in range(RETRY_ATTEMPTS):
            try:
                # Всегда задаём fee_payer и свежий блокхэш
                blockhash, last_valid_block_height = await self._latest_blockhash(use_fallback=use_fallback)
                tx.recent_blockhash = blockhash
                if tx.fee_payer is None:
                    tx.fee_payer = self.sender_pubkey

                opts = TxOpts(skip_preflight=not preflight, preflight_commitment=DEFAULT_COMMITMENT, max_retries=3)

                resp = await client.send_transaction(tx, self.sender_keypair, opts=opts)
                sig = str(resp.value)
                self.logger.info("Транзакция отправлена: %s (попытка %d/%d)", sig, attempt + 1, RETRY_ATTEMPTS)

                ok = await self.confirm_transaction(sig, commitment=commitment, last_valid_block_height=last_valid_block_height, use_fallback=use_fallback)
                if ok:
                    return sig
            except Exception as e:
                self.logger.warning("_send_and_confirm attempt %d failed: %s", attempt + 1, e)
            await asyncio.sleep(RETRY_DELAY * (2 ** attempt))  # Экспоненциальный backoff
        # Пробуем fallback если не удалось
        if not use_fallback:
            self.logger.info("Пробуем fallback RPC для _send_and_confirm")
            return await self._send_and_confirm(tx, preflight=preflight, commitment=commitment, use_fallback=True)
        return None

    async def confirm_transaction(
        self,
        signature: str,
        *,
        commitment: Commitment = Finalized,
        last_valid_block_height: Optional[int] = None,
        use_fallback: bool = False,
    ) -> bool:
        """Подтверждение завершения транзакции с ретраями и fallback (на enum)."""
        client = self.fallback_client if use_fallback else self.client
        sig = Signature.from_string(signature)
        self.logger.info("Подтверждение транзакции: %s", sig)

        for attempt in range(RETRY_ATTEMPTS):
            try:
                st = await client.get_signature_statuses([sig], search_transaction_history=True)
                status_entry = st.value[0] if st.value else None

                if status_entry is None:
                    self.logger.info("Статус ещё не доступен (попытка %d/%d).", attempt + 1, RETRY_ATTEMPTS)

                else:
                    # Проверка блокхэша (не протух ли)
                    if last_valid_block_height is not None and status_entry.confirmation_status is None:
                        hb = await client.get_block_height()
                        if hb.value > last_valid_block_height:
                            self.logger.error("Blockhash протух до подтверждения: %s", signature)
                            return False

                    # Ошибка в транзакции
                    if status_entry.err is not None:
                        self.logger.error("Транзакция %s завершилась с ошибкой: %s", signature, status_entry.err)
                        return False

                    # Проверяем статус через enum
                    conf = status_entry.confirmation_status
                    if conf is not None:
                        if (commitment is Finalized and conf == TransactionConfirmationStatus.Finalized) or \
                        (commitment is Confirmed and conf in (TransactionConfirmationStatus.Confirmed, TransactionConfirmationStatus.Finalized)) or \
                        (commitment is Processed and conf in (TransactionConfirmationStatus.Processed, TransactionConfirmationStatus.Confirmed, TransactionConfirmationStatus.Finalized)):
                            self.logger.info("Транзакция %s подтверждена (%s).", signature, conf)
                            return True

                await asyncio.sleep(RETRY_DELAY * (2 ** attempt))

            except Exception as e:
                self.logger.warning("Ошибка проверки статуса %s (попытка %d): %s", signature, attempt + 1, e)
                await asyncio.sleep(RETRY_DELAY * (2 ** attempt))

        # Пробуем fallback если не удалось
        if not use_fallback:
            self.logger.info("Пробуем fallback RPC для confirm_transaction")
            return await self.confirm_transaction(
                signature,
                commitment=commitment,
                last_valid_block_height=last_valid_block_height,
                use_fallback=True,
            )

        self.logger.error("Транзакция %s не подтверждена за %d попыток.", signature, RETRY_ATTEMPTS)
        return False

    async def get_transaction_fee(self, tx_instructions: list, use_fallback: bool = False) -> int:
        """
        Оценка комиссии за транзакцию (lamports) с ретраями и fallback.
        tx_instructions: список инструкций (Instruction) для транзакции
        """
        client = self.fallback_client if use_fallback else self.client
        for attempt in range(RETRY_ATTEMPTS):
            try:
                blockhash, _ = await self._latest_blockhash(use_fallback=use_fallback)

                # Создаем сообщение и версионированную транзакцию
                message = MessageV0.try_compile(
                    payer=self.sender_pubkey,
                    instructions=tx_instructions,
                    address_lookup_table_accounts=[],
                    recent_blockhash=blockhash
                )
                transaction = VersionedTransaction(message, [self.sender_keypair])

                # Получаем fee
                resp = await client.get_fee_for_message(message)
                fee = int(resp.value or 0)
                if fee <= 0:
                    raise RuntimeError("fee==0")

                self.logger.info("Оценочная комиссия: %d лампортов", fee)
                return fee
            except Exception as e:
                self.logger.warning("get_transaction_fee attempt %d failed: %s", attempt + 1, e)
                await asyncio.sleep(RETRY_DELAY * (2 ** attempt))

        # Fallback
        if not use_fallback:
            return await self.get_transaction_fee(tx_instructions, use_fallback=True)
        return MIN_SOL_RESERVE

    async def get_token_decimals(self, mint: Pubkey, use_fallback: bool = False) -> int:
        """Получить decimals токена с ретраями и fallback."""
        client = self.fallback_client if use_fallback else self.client
        for attempt in range(RETRY_ATTEMPTS):
            try:
                r = await client.get_token_supply(mint)
                return int(r.value.decimals)
            except Exception as e:
                self.logger.warning("get_token_decimals attempt %d failed: %s", attempt + 1, e)
                await asyncio.sleep(RETRY_DELAY * (2 ** attempt))
        if not use_fallback:
            return await self.get_token_decimals(mint, use_fallback=True)
        raise RuntimeError(f"Failed to get decimals for {mint}")

    async def get_token_amount_raw(self, ata: Pubkey, use_fallback: bool = False) -> int:
        """Получить raw amount токена с ретраями и fallback."""
        client = self.fallback_client if use_fallback else self.client
        for attempt in range(RETRY_ATTEMPTS):
            try:
                bal = await client.get_token_account_balance(ata)
                return int(bal.value.amount)
            except Exception as e:
                self.logger.warning("get_token_amount_raw attempt %d failed: %s", attempt + 1, e)
                await asyncio.sleep(RETRY_DELAY * (2 ** attempt))
        if not use_fallback:
            return await self.get_token_amount_raw(ata, use_fallback=True)
        return 0

    async def get_balance(self, pubkey: Pubkey, use_fallback: bool = False) -> int:
        """Получить SOL баланс (lamports) с ретраями и fallback."""
        client = self.fallback_client if use_fallback else self.client
        for attempt in range(RETRY_ATTEMPTS):
            try:
                r = await client.get_balance(pubkey, commitment=Confirmed)
                return r.value
            except Exception as e:
                self.logger.warning("get_balance attempt %d failed: %s", attempt + 1, e)
                await asyncio.sleep(RETRY_DELAY * (2 ** attempt))
        if not use_fallback:
            return await self.get_balance(pubkey, use_fallback=True)
        return 0

    async def is_token_account_open(self, owner_pubkey: Pubkey, token_mint_pubkey: Pubkey, use_fallback: bool = False) -> bool:
        """Проверяем наличие АТА без лишних вызовов токен-клиента с ретраями и fallback."""
        client = self.fallback_client if use_fallback else self.client
        ata = get_associated_token_address(owner_pubkey, token_mint_pubkey)
        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = await client.get_account_info(ata, commitment=DEFAULT_COMMITMENT)
                is_open = resp.value is not None
                self.logger.info("ATA %s открыт: %s", ata, is_open)
                return is_open
            except Exception as e:
                self.logger.warning("is_token_account_open attempt %d failed: %s", attempt + 1, e)
                await asyncio.sleep(RETRY_DELAY * (2 ** attempt))
        if not use_fallback:
            return await self.is_token_account_open(owner_pubkey, token_mint_pubkey, use_fallback=True)
        return False

    # ---------- Операции с токенами и SOL ----------

    async def transfer_sol(self, destination: Pubkey, amount: int) -> Optional[str]:
        """Перевод SOL (lamports) с заполнением blockhash/fee_payer и нормальным подтверждением."""
        self.logger.info("Инициируется перевод %d лампортов SOL на %s", amount, destination)
        tx = Transaction(fee_payer=self.sender_pubkey)
        tx.add(
            transfer_sol(
                TransferParamsSol(
                    from_pubkey=self.sender_pubkey,
                    to_pubkey=destination,
                    lamports=amount,
                )
            )
        )
        return await self._send_and_confirm(tx, preflight=True, commitment=Finalized)
    
    async def transfer_sol_commision(self, destination: Pubkey, amount_raw: int) -> Optional[str]:
        # Оценим комиссию для реальной транзакции перевода
        probe_tx = transfer_sol(
            TransferParamsSol(
                from_pubkey=self.sender_pubkey,
                to_pubkey=destination,
                lamports=1,  # Заглушка для оценки
            )
        )
        sol_fee = await self.get_transaction_fee([probe_tx])
        self.logger.info("Оценочная комиссия: %d лампортов", sol_fee)

        # Рассчитываем отправляемую сумму: весь баланс за вычетом комиссии
        sendable = amount_raw - sol_fee
        if sendable <= 0:
            self.logger.warning(
                "Недостаточно SOL для перевода: баланс=%d, комиссия=%d",
                amount_raw,
                sol_fee,
            )
            return None

        # Проверяем, что остаток после транзакции будет ровно 0 или больше 0.002
        remaining_balance = amount_raw - sendable - sol_fee
        if remaining_balance != 0 and remaining_balance<2000000:
            self.logger.error(
                "Невозможно отправить SOL: остаток (%d) меньше 0.002 sol. Проверьте расчёт комиссии.",
                remaining_balance
            )
            return None
        
        sol_sig = await self.transfer_sol(destination, sendable)
        if not sol_sig:
            self.logger.error("Не удалось перевести SOL.")
            return None

        self.logger.info("Готово. Транзакция SOL: %s", sol_sig)
        return sol_sig

    async def transfer_token(self, destination: Pubkey, token_mint:str, amount_raw: int) -> Optional[str]:
        """Перевод TOKEN (UI) с заполнением blockhash/fee_payer и нормальным подтверждением."""
        self.logger.info("Инициируется перевод %d TOKEN на %s", amount_raw, destination)
        sender_ata = get_associated_token_address(self.sender_pubkey, token_mint)
        receiver_ata = get_associated_token_address(destination, token_mint)
        tx = Transaction(fee_payer=self.sender_pubkey)
        if not await self.is_token_account_open(destination, token_mint):
            self.logger.info("Создание АТА получателя: %s", receiver_ata)
            tx.add(
                create_associated_token_account(
                    payer=self.sender_pubkey,
                    owner=destination,
                    mint=token_mint,
                )
            )        
        tx.add(
            spl_transfer(
                TransferParams(
                    program_id=TOKEN_PROGRAM_ID,
                    source=sender_ata,
                    dest=receiver_ata,
                    owner=self.sender_pubkey,
                    amount=amount_raw,
                )
            )
        )
        return await self._send_and_confirm(tx, preflight=True, commitment=Finalized)

    async def wait_for_token_balance(self, sender_token_account: Pubkey) -> Optional[int]:
        """
        Ждём появления/наличия токенов на ATA отправителя.
        Возвращает ЦЕЛОЕ количество в минимальных единицах.
        """
        self.logger.info("Ожидание токенов на: %s", sender_token_account)
        deadline = time.time() + MAX_TIMEOUT
        while time.time() < deadline:
            try:
                amount_raw = await self.get_token_amount_raw(sender_token_account)
                if amount_raw > 0:
                    self.logger.info("Токены получены: %d (минимальные единицы)", amount_raw)
                    return amount_raw
                self.logger.info("Токенов нет, подождём...")
                await asyncio.sleep(random.uniform(5, 10))
            except Exception as e:
                self.logger.error("Ошибка проверки баланса токенов: %s", e)
                await asyncio.sleep(random.uniform(5, 10))
        self.logger.error("Токены не получены за %d секунд", MAX_TIMEOUT)
        return None

    # ---------- Новая функция: Обмен токенов через Jupiter (как в wallet_interface) ----------

    async def jup_quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int = 50, swapMode: str = "ExactIn") -> Dict[str, Any]:
        """Получить котировку от Jupiter с ретраями."""
        for attempt in range(RETRY_ATTEMPTS):
            try:
                params = {
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": amount,
                    "slippageBps": slippage_bps,
                    "swapMode": swapMode,
                }
                r = requests.get(JUPITER_QUOTE_URL, params=params, timeout=20)
                if not("error" in r.text):
                    r.raise_for_status()
                return r.json()
            except requests.exceptions.RequestException as e:
                self.logger.warning("jup_quote attempt %d failed: %s", attempt + 1, e)
                if attempt < RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(RETRY_DELAY * (2 ** attempt))
        raise RuntimeError("Failed to fetch Jupiter quote after retries")

    async def swap_tokens(
        self,
        input_mint: str,
        output_mint: str,
        amount_ui: float,
        slippage_bps: int = 50,
        amount_is_input: bool = True,
        target_output_ui: float = None,
        max_output_tolerance: float = 0.01  # Допуск 1% сверху
    ) -> Optional[str]:
        """
        Обмен токенов через Jupiter. Поддерживает ExactIn (если amount_is_input=True) и ExactOut
        (если amount_is_input=False и токен поддерживает). При ошибке ExactOut переключается на ExactIn
        с подбором входного количества SOL для достижения target_output_ui (или до +max_output_tolerance).

        :param input_mint: Mint токена ввода (например, SOL).
        :param output_mint: Mint токена вывода.
        :param amount_ui: Количество токенов ввода (если amount_is_input=True) или игнорируется.
        :param slippage_bps: Скольжение в базисных пунктах (например, 50 = 0.5%).
        :param amount_is_input: Если True, использует ExactIn с amount_ui; если False, пробует ExactOut или ExactIn с подбором.
        :param target_output_ui: Целевое количество токенов вывода в UI-единицах (если amount_is_input=False).
        :param max_output_tolerance: Максимальный допуск сверху для выходного количества (например, 0.01 = 1%).
        :return: Подпись транзакции или None при ошибке.
        """
        self.logger.info(
            "Инициируется своп: %s -> %s, amount_ui=%f, slippage_bps=%d, amount_is_input=%s, target_output_ui=%s",
            input_mint, output_mint, amount_ui, slippage_bps, amount_is_input, target_output_ui
        )

        for use_fallback in [False, True]:
            try:
                # Получаем decimals для токенов
                in_dec = await self.get_token_decimals(Pubkey.from_string(input_mint)) if input_mint != WSOL_MINT else 9
                out_dec = await self.get_token_decimals(Pubkey.from_string(output_mint))

                if amount_is_input:
                    # Стандартный ExactIn своп
                    amount_raw = int(round(amount_ui * (10 ** in_dec)))
                    quote = await self.jup_quote(input_mint, output_mint, amount_raw, slippage_bps, swapMode="ExactIn")
                else:
                    # Проверяем, что target_output_ui указано
                    if target_output_ui is None:
                        self.logger.error("target_output_ui должен быть указан, если amount_is_input=False")
                        return None

                    target_output_raw = int(round(target_output_ui * (10 ** out_dec)))
                    max_output_raw = int(round(target_output_raw * (1 + max_output_tolerance)))

                    # Пробуем ExactOut
                    try:
                        quote = await self.jup_quote(
                            input_mint, output_mint, target_output_raw, slippage_bps, swapMode="ExactOut"
                        )
                        if 'error' in quote:
                            raise Exception("COULD_NOT_FIND_ANY_ROUTE")
                        self.logger.info("ExactOut успешен: получена котировка для %d токенов", target_output_raw)
                    except Exception as e:
                        # Проверяем, связана ли ошибка с отсутствием маршрута для ExactOut
                        if 'COULD_NOT_FIND_ANY_ROUTE' in str(e):
                            self.logger.info("ExactOut не поддерживается, переключаемся на ExactIn с подбором")
                            # Итеративный подбор для ExactIn
                            low_sol = 1_000_000  # 0.001 SOL
                            high_sol = 500_000_000  # 0.5 SOL
                            max_iterations = 20
                            best_amount_raw = None
                            best_quote = None

                            for _ in range(max_iterations):
                                mid_sol = (low_sol + high_sol) // 2
                                quote = await self.jup_quote(
                                    input_mint, output_mint, mid_sol, slippage_bps, swapMode="ExactIn"
                                )
                                out_amount = int(quote["outAmount"])

                                self.logger.info(
                                    "Попытка ExactIn: input=%d lamports, output=%d (target=%d, max=%d)",
                                    mid_sol, out_amount, target_output_raw, max_output_raw
                                )

                                if target_output_raw <= out_amount <= max_output_raw:
                                    # Подходящее количество найдено
                                    best_amount_raw = mid_sol
                                    best_quote = quote
                                    break
                                elif out_amount < target_output_raw:
                                    # Слишком мало токенов, увеличиваем SOL
                                    low_sol = mid_sol + 1
                                else:
                                    # Слишком много токенов, уменьшаем SOL
                                    high_sol = mid_sol - 1

                                if high_sol - low_sol < 1000:  # Точность до 0.000001 SOL
                                    self.logger.info("Достигнута максимальная точность поиска")
                                    break

                            if best_amount_raw is None:
                                self.logger.error("Не удалось найти подходящее количество SOL для целевого вывода")
                                return None

                            self.logger.info(
                                "Найдено: input=%d lamports для получения ~%f токенов",
                                best_amount_raw, int(best_quote["outAmount"]) / (10 ** out_dec)
                            )
                            quote = best_quote
                            amount_raw = best_amount_raw
                        else:
                            # Другая ошибка, пробуем fallback
                            raise e
                # Проверяем баланс SOL перед свопом
                sol_balance = await self.get_balance(self.sender_pubkey)
                probe_tx = transfer_sol(
                    TransferParamsSol(
                        from_pubkey=self.sender_pubkey,
                        to_pubkey=self.sender_pubkey,
                        lamports=1
                    )
                )
                sol_fee = await self.get_transaction_fee([probe_tx])
                required_sol = 0
                if input_mint==WSOL_MINT:
                    required_sol = int(quote["inAmount"]) if not amount_is_input else amount_raw
                if sol_balance < required_sol + sol_fee:
                    self.logger.error(
                        "Недостаточно SOL: баланс=%d, нужно=%d + комиссия=%d",
                        sol_balance, required_sol, sol_fee
                    )
                    return None

                # Выполняем своп
                payload = {
                    "userPublicKey": str(self.sender_pubkey),
                    "quoteResponse": quote,
                    "wrapAndUnwrapSol": True,
                    "asLegacyTransaction": False,
                    "useSharedAccounts": True,
                    "dynamicComputeUnitLimit": True,
                    "dynamicSlippage": False
                }
                r = requests.post(JUPITER_SWAP_URL, json=payload, timeout=30)
                r.raise_for_status()
                swap_tx_b64 = r.json()["swapTransaction"]
                raw = base64.b64decode(swap_tx_b64)
                tx = VersionedTransaction.from_bytes(raw)
                signature = self.sender_keypair.sign_message(to_bytes_versioned(tx.message))
                signed_tx = VersionedTransaction.populate(tx.message, [signature])
                client = self.fallback_client if use_fallback else self.client
                opts = TxOpts(skip_preflight=False, preflight_commitment=DEFAULT_COMMITMENT)
                resp = await client.send_raw_transaction(bytes(signed_tx), opts=opts)
                s = str(resp.value)
                ok = await self.confirm_transaction(s, commitment=Finalized, use_fallback=use_fallback)
                if ok:
                    self.logger.info("Своп успешен: %s", s)
                    return s
            except Exception as e:
                self.logger.warning("swap_tokens failed (fallback=%s): %s", use_fallback, e)
        return None

    async def close_ata_account(self, ata, pub_str) -> Optional[str]:
        # Закрываем ATA
        tx_close = Transaction(fee_payer=Pubkey.from_string(pub_str)).add(
            close_account(
                CloseAccountParams(
                    program_id=TOKEN_PROGRAM_ID,
                    account=ata,
                    dest=Pubkey.from_string(pub_str),
                    owner=Pubkey.from_string(pub_str),
                )
            )
        )
        sig_close = await self._send_and_confirm(tx_close, preflight=True, commitment=Finalized)
        return sig_close
        

    async def transfer_and_close_spl_token_account(self, birge_wallet: str, wallet_2: str) -> Optional[str]:
        """
        1) Создать АТА получателя при необходимости
        2) Перевести ВСЕ токены на биржевой кошелёк
        3) Закрыть АТА отправителя (вернёт ренту на основной счёт)
        4) Перевести почти все SOL на второй кошелёк (оставив MIN_SOL_RESERVE и комиссию)
        """
        if await self.client.is_connected():
            self.logger.info("RPC OK: %s", RPC)
        else:
            self.logger.error("Нет соединения с RPC: %s", RPC)
            return None

        await asyncio.sleep(random.uniform(0.5, 1.5))  # немного «джиттера», если это батч

        birge_pubkey = Pubkey.from_string(birge_wallet)
        wallet_2_pubkey = Pubkey.from_string(wallet_2)

        token = AsyncToken(self.client, self.token_mint_pubkey, TOKEN_PROGRAM_ID, self.sender_keypair)
        sender_ata = get_associated_token_address(self.sender_pubkey, self.token_mint_pubkey)
        receiver_ata = get_associated_token_address(birge_pubkey, self.token_mint_pubkey)

        # 0) Убедимся, что АТА получателя существует
        if not await self.is_token_account_open(birge_pubkey, self.token_mint_pubkey):
            self.logger.info("Создание АТА получателя: %s", receiver_ata)
            tx_create = Transaction(fee_payer=self.sender_pubkey).add(
                create_associated_token_account(
                    payer=self.sender_pubkey,
                    owner=birge_pubkey,
                    mint=self.token_mint_pubkey,
                )
            )
            sig = await self._send_and_confirm(tx_create, preflight=True, commitment=Finalized)
            if not sig:
                self.logger.error("Не удалось создать АТА получателя.")
                return None

        # 1) Ждём токены и берём ЦЕЛОЕ значение
        amount_raw = await self.wait_for_token_balance(sender_ata)
        if not amount_raw:
            self.logger.error("Нет токенов для перевода.")
            return None

        # 2) Единая транзакция: перевод токенов и закрытие АТА отправителя
        tx_tok = Transaction(fee_payer=self.sender_pubkey)
        tx_tok.add(
            spl_transfer(
                TransferParams(
                    program_id=TOKEN_PROGRAM_ID,
                    source=sender_ata,
                    dest=receiver_ata,
                    owner=self.sender_pubkey,
                    amount=amount_raw,
                )
            )
        )
        tx_tok.add(
            close_account(
                CloseAccountParams(
                    program_id=TOKEN_PROGRAM_ID,
                    account=sender_ata,
                    dest=self.sender_pubkey,  # рента вернётся сюда
                    owner=self.sender_pubkey,
                )
            )
        )

        sig_tc = await self._send_and_confirm(tx_tok, preflight=True, commitment=Finalized)
        if not sig_tc:
            self.logger.error("Не удалось перевести токены и закрыть АТА.")
            return None

        # 3) Проверяем, что АТА реально закрыт
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            is_open = await self.is_token_account_open(self.sender_pubkey, self.token_mint_pubkey)
            if not is_open:
                self.logger.info("АТА закрыт.")
                break
            self.logger.warning("АТА всё ещё открыт (попытка %d/%d), ждём...", attempt, RETRY_ATTEMPTS)
            await asyncio.sleep(RETRY_DELAY)
        else:
            self.logger.error("АТА не удалось закрыть после %d попыток.", RETRY_ATTEMPTS)
            return None

        # Небольшая пауза, чтобы рента докатилась
        await asyncio.sleep(2)

        # 4) Переводим весь SOL, оставляя только комиссию
        sol_balance = await self.get_balance(self.sender_pubkey)
        self.logger.info("Текущий баланс SOL: %d лампортов", sol_balance)

        # Оценим комиссию для реальной транзакции перевода
        probe_tx = transfer_sol(
            TransferParamsSol(
                from_pubkey=self.sender_pubkey,
                to_pubkey=wallet_2_pubkey,
                lamports=1,  # Заглушка для оценки
            )
        )
        sol_fee = await self.get_transaction_fee([probe_tx])
        self.logger.info("Оценочная комиссия: %d лампортов", sol_fee)

        # Рассчитываем отправляемую сумму: весь баланс за вычетом комиссии
        sendable = sol_balance - sol_fee
        if sendable <= 0:
            self.logger.warning(
                "Недостаточно SOL для перевода: баланс=%d, комиссия=%d",
                sol_balance,
                sol_fee,
            )
            return None

        # Проверяем, что остаток после транзакции будет ровно 0
        remaining_balance = sol_balance - sendable - sol_fee
        if remaining_balance != 0:
            self.logger.error(
                "Невозможно отправить весь SOL: остаток (%d) не равен 0. Проверьте расчёт комиссии.",
                remaining_balance
            )
            return None

        self.logger.info("Переводим весь SOL: %d лампортов на %s", sendable, wallet_2_pubkey)
        sol_sig = await self.transfer_sol(wallet_2_pubkey, sendable)
        if not sol_sig:
            self.logger.error("Не удалось перевести SOL.")
            return None

        self.logger.info("Готово. Транзакция SOL: %s", sol_sig)
        return sol_sig