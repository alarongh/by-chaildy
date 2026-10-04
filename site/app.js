(() => {
  'use strict';
  const config = window.CHAILDY || {};
  const dialog = document.querySelector('#booking-dialog');
  const chat = document.querySelector('#demo-chat');
  const next = document.querySelector('#demo-next');
  const usernameOK = value => /^[A-Za-z][A-Za-z0-9_]{4,31}$/.test(value || '');
  const example = [
    ['bot', 'Привет! Я помогу оставить заявку by:Chaildy. Перед анкетой покажу отдельное согласие на обработку данных.'],
    ['user', 'Согласен / согласна с обработкой данных. Мне исполнилось 18 лет.'],
    ['bot', '1 / 5 · Как к тебе обращаться?'], ['user', 'Аня'],
    ['bot', '2 / 5 · Какая у тебя идея?'], ['user', 'Небольшая веточка. Чёрная, с тонкими линиями.'],
    ['bot', '3 / 5 · Где хочешь тату?'], ['user', 'Левое предплечье'],
    ['bot', '4 / 5 · Какой примерный размер?'], ['user', 'Около 8 см'],
    ['bot', '5 / 5 · Когда тебе удобно?'], ['user', 'В выходные после обеда'],
    ['bot', 'Бюджет и референсы можно добавить по желанию. Перед отправкой можно проверить и изменить каждый ответ.'],
    ['user', 'Всё верно. Отправить мастеру.'],
    ['bot', 'Заявка сохранена. Мастер обсудит детали и подтвердит дату отдельно. До этого сеанс не забронирован.']
  ];
  let cursor = 0;
  let opener = null;
  function openPreview(event) {
    opener = event?.currentTarget || document.activeElement;
    cursor = 0;
    chat.replaceChildren();
    chat.hidden = true;
    next.textContent = 'Посмотреть пример анкеты →';
    next.disabled = false;
    dialog.showModal();
  }
  function booking(event) {
    if (usernameOK(config.botUsername)) {
      window.open(`https://t.me/${config.botUsername}?start=website`, '_blank', 'noopener,noreferrer');
    } else {
      openPreview(event);
    }
  }
  document.querySelectorAll('[data-book]').forEach(button => button.addEventListener('click', booking));
  document.querySelector('#preview-link').addEventListener('click', openPreview);
  document.querySelector('.dialog-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', event => {
    const rect = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) dialog.close();
  });
  dialog.addEventListener('close', () => opener?.focus());
  next.addEventListener('click', () => {
    chat.hidden = false;
    const stop = Math.min(cursor + 2, example.length);
    for (; cursor < stop; cursor++) {
      const [role, text] = example[cursor];
      const bubble = document.createElement('div');
      bubble.className = `chat-bubble ${role}`;
      const label = document.createElement('small');
      label.textContent = role === 'bot' ? 'by:Chaildy · бот' : 'Пример ответа';
      bubble.append(label, document.createTextNode(text));
      chat.append(bubble);
    }
    chat.scrollTop = chat.scrollHeight;
    next.textContent = cursor < example.length ? 'Следующий шаг →' : 'Пример завершён';
    next.disabled = cursor === example.length;
  });
  if (config.city) document.querySelector('#city-label').textContent = `${config.city} · По записи · 18+`;
  document.querySelector('#year').textContent = new Date().getFullYear();
  function safeImage(value) {
    try {
      const url = new URL(value, location.href);
      return url.origin === location.origin && /\.(avif|webp|jpe?g|png)$/i.test(url.pathname) ? url.href : null;
    } catch { return null; }
  }
  async function loadGallery() {
    try {
      const response = await fetch(config.galleryUrl || './content/gallery.json', {cache:'no-cache'});
      if (!response.ok) throw new Error('Gallery unavailable');
      const items = await response.json();
      if (!Array.isArray(items)) return;
      const gallery = document.querySelector('#gallery');
      for (const item of items.slice(0, 60)) {
        const imageUrl = safeImage(item.image);
        if (!imageUrl || typeof item.title !== 'string' || item.published === false) continue;
        const card = document.createElement('article');
        card.className = 'gallery-card';
        const image = document.createElement('img');
        image.src = imageUrl; image.alt = item.title; image.loading = 'lazy';
        const title = document.createElement('h3'); title.textContent = item.title;
        const description = document.createElement('p'); description.textContent = item.description || '';
        const button = document.createElement('button'); button.className = 'text-button';
        button.textContent = item.available === false ? 'Эскиз занят' : 'Обсудить этот эскиз ↗︎';
        if (item.available === false) button.disabled = true; else button.addEventListener('click', booking);
        card.append(image, title, description, button); gallery.append(card);
      }
      if (gallery.childElementCount) { gallery.hidden = false; document.querySelector('#gallery-empty').hidden = true; }
    } catch { /* The honest empty state is also the fallback for an unavailable catalogue. */ }
  }
  loadGallery();
})();
