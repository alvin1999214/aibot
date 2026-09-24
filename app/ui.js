async function refresh() {
  try {
    const response = await fetch('/status');
    if (!response.ok) throw new Error('無法讀取狀態，請重新登入管理頁');
    const data = await response.json();
    document.querySelector('#status').textContent = `${data.state}\n帳號：${data.username || '—'}\n最近錯誤：${data.error || '無'}`;
    document.querySelector('#challenge').hidden = !data.challenge;
  } catch (error) {
    document.querySelector('#status').textContent = error.message;
  }
}
for (const name of ['login', 'challenge']) {
  document.getElementById(name).addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.target;
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      const response = await fetch(`/${name}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Bot-Admin': '1'},
        body: JSON.stringify(Object.fromEntries(new FormData(form))),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '輸入格式錯誤');
      document.querySelector('#result').textContent = data.message;
      form.reset();
      await refresh();
    } catch (error) {
      document.querySelector('#result').textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
}
refresh();
setInterval(refresh, 3000);
