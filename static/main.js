// main.js
function toggleTheme() {
  document.body.classList.toggle('dark-theme');
  document.body.classList.toggle('light-theme');
}

async function uploadWallets() {
  const file = document.getElementById('walletsFile').files[0];
  if (!file) { alert('Выбери CSV'); return; }
  const fd = new FormData();
  fd.append('file', file);
  const r = await fetch('/upload_wallets', {method: 'POST', body: fd});
  const j = await r.json();
  alert(j.detail || 'OK');
  refreshWallets();
}

async function uploadTokens() {
  const file = document.getElementById('tokensFile').files[0];
  if (!file) { alert('Выбери CSV'); return; }
  const fd = new FormData();
  fd.append('file', file);
  const r = await fetch('/upload_tokens', {method: 'POST', body: fd});
  const j = await r.json();
  alert(j.detail || 'OK');
  fetchTokens();
}

async function fetchTokens() {
  const r = await fetch('/tokens');
  const tokens = await r.json();
  const selects = ['inputToken', 'outputToken', 'closeToken', 'transferToken'];
  selects.forEach(id => {
    const sel = document.getElementById(id);
    sel.innerHTML = '<option>SOL</option><option>USDC</option>';
    tokens.forEach(t => {
      const opt = document.createElement('option');
      opt.value = t.mint;
      opt.text = t.name;
      sel.appendChild(opt);
    });
  });
}

async function refreshWallets() {
  const r = await fetch('/wallets/full_status');
  const arr = await r.json();
  const selects = ['walletSelect', 'swapWalletSelect', 'closeWalletSelect', 'transferWalletSelect'];
  selects.forEach(id => {
    const sel = document.getElementById(id);
    sel.innerHTML = '';
    arr.forEach((w, i) => {
      const opt = document.createElement('option');
      opt.value = i;
      opt.text = `${i}: ${w.public_key.slice(0, 8)}...`;
      sel.appendChild(opt);
    });
  });
  if (arr.length) showWalletBalance(0);
}

function showWalletBalance(index) {
  const r = fetch('/wallets/full_status').then(res => res.json()).then(arr => {
    const wallet = arr[index];
    const tbody = document.querySelector('#balanceTable tbody');
    tbody.innerHTML = '';
    Object.keys(wallet.balances).forEach(key => {
      const row = document.createElement('tr');
      row.innerHTML = `<td>${key}</td><td>${wallet.balances[key]?.toFixed(4) || 0}</td><td>${wallet[`ATA_${key}_open`] || false}</td>`;
      tbody.appendChild(row);
    });
  });
}

async function performSwap() {
  const wallet_index = document.getElementById('swapWalletSelect').value;
  const input_mint = document.getElementById('inputToken').value === 'SOL' ? 'So11111111111111111111111111111111111111112' : 
                     document.getElementById('inputToken').value === 'USDC' ? 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v' : 
                     document.getElementById('inputToken').value;
  const output_mint = document.getElementById('outputToken').value === 'SOL' ? 'So11111111111111111111111111111111111111112' : 
                      document.getElementById('outputToken').value === 'USDC' ? 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v' : 
                      document.getElementById('outputToken').value;
  const amount_ui = parseFloat(document.getElementById('swapAmount').value);
  const r = await fetch('/swap', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ wallet_index, input_mint, output_mint, amount_ui })
  });
  const j = await r.json();
  alert(j.signature || j.detail);
  refreshWallets();
}

async function closeATA() {
  const wallet_index = document.getElementById('closeWalletSelect').value;
  const token_mint = document.getElementById('closeToken').value === 'SOL' ? 'So11111111111111111111111111111111111111112' : 
                     document.getElementById('closeToken').value === 'USDC' ? 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v' : 
                     document.getElementById('closeToken').value;
  const r = await fetch('/close_ata', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ wallet_index, token_mint })
  });
  const j = await r.json();
  alert(j.signature || j.detail);
  refreshWallets();
}

async function performTransfer() {
  const wallet_index = document.getElementById('transferWalletSelect').value;
  const token_mint = document.getElementById('transferToken').value;
  const recipient = document.getElementById('recipient').value;
  const amount_ui = parseFloat(document.getElementById('transferAmount').value);
  const r = await fetch('/transfer', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ wallet_index, token_mint, recipient, amount_ui })
  });
  const j = await r.json();
  alert(j.signature || j.detail);
  refreshWallets();
}

async function refreshLogs() {
  const r = await fetch('/logs');
  const j = await r.json();
  document.getElementById('logs').innerText = j.join('\n');
}

setInterval(refreshLogs, 1500);
fetchTokens();
refreshWallets();