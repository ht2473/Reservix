/*!
 * Reservix — система формирования и развития кадрового резерва
 *
 * ВНИМАНИЕ: файл собран автоматически из static/src/js/.
 * Не редактируйте его напрямую — правки будут потеряны при следующей сборке.
 * Источники: 01-core.js, 02-ui.js, 03-palette.js, 04-portal.js
 */

/* ===== 01-core.js ======================================================= */
/* Reservix · Базовый слой сценариев */

(function () {
  "use strict";

  /* Пространство имён приложения: избавляет от засорения глобальной области. */
  const Reservix = (window.Reservix = window.Reservix || {});

  /* ======================================================================
     Подписи интерфейса
     ====================================================================== */

  /* Словарь переводимых подписей. */
  let strings = {};

  try {
    const holder = document.getElementById("ui-strings");
    if (holder) strings = JSON.parse(holder.textContent) || {};
  } catch (error) {
    strings = {};
  }

  /* Возвращает подпись по ключу. */
  function t(key, fallback) {
    const value = strings[key];
    return typeof value === "string" && value ? value : fallback || key;
  }

  Reservix.t = t;

  /* Оформляет число по правилам действующего языка. */
  function formatNumber(value, digits) {
    const fixed = Number(value).toFixed(digits === undefined ? 0 : digits);
    const parts = fixed.split(".");
    const group = t("number.group", " ");
    const decimal = t("number.decimal", ",");
    const whole = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, group);
    return parts.length > 1 ? whole + decimal + parts[1] : whole;
  }

  Reservix.formatNumber = formatNumber;

  /* ======================================================================
     Тема оформления и плотность таблиц
     ====================================================================== */

  const APPEARANCE_COOKIE = "reservix_appearance";
  const THEME_KEY = "reservix:theme";
  const VALID_THEMES = ["light", "dark", "system"];
  const VALID_DENSITIES = ["comfortable", "compact"];

  /* Читает значение cookie по имени. */
  function getCookie(name) {
    const match = document.cookie.match(
      new RegExp("(^|;\\s*)" + name + "=([^;]*)")
    );
    return match ? decodeURIComponent(match[2]) : null;
  }

  /* Сохраняет оформление в cookie. */
  function persistAppearance(theme, density) {
    const value = theme + ":" + density;
    const secure = window.location.protocol === "https:" ? "; Secure" : "";
    document.cookie =
      APPEARANCE_COOKIE +
      "=" +
      encodeURIComponent(value) +
      "; Path=/; Max-Age=31536000; SameSite=Lax" +
      secure;
  }

  function currentAppearance() {
    const raw = getCookie(APPEARANCE_COOKIE) || "";
    const parts = raw.split(":");
    const theme = VALID_THEMES.includes(parts[0]) ? parts[0] : readRootTheme();
    const density = VALID_DENSITIES.includes(parts[1]) ? parts[1] : readRootDensity();
    return { theme: theme, density: density };
  }

  /* Тема, уже применённая к разметке сервером. */
  function readRootTheme() {
    return document.documentElement.getAttribute("data-theme") || "system";
  }

  function readRootDensity() {
    return document.documentElement.getAttribute("data-density") === "compact"
      ? "compact"
      : "comfortable";
  }

  /* Применяет тему к корневому элементу документа. */
  function applyTheme(theme) {
    const root = document.documentElement;
    if (theme === "system" || !VALID_THEMES.includes(theme)) {
      root.removeAttribute("data-theme");
    } else {
      root.setAttribute("data-theme", theme);
    }
    updateThemeControls(theme);
  }

  /* Синхронизирует состояние переключателей темы в интерфейсе. */
  function updateThemeControls(theme) {
    document.querySelectorAll("[data-theme-option]").forEach(function (button) {
      const isCurrent = button.dataset.themeOption === theme;
      button.setAttribute("aria-pressed", String(isCurrent));
      button.classList.toggle("is-current", isCurrent);
    });
  }

  /* Переносит выбор в профиль пользователя. */
  function syncAppearance(theme, density) {
    const endpoint = document.body.dataset.appearanceUrl;
    const token = getCookie("reservix_csrf");
    if (!endpoint || !token || !window.fetch) return;

    const payload = new URLSearchParams();
    payload.set("theme", theme);
    payload.set("density", density);

    window
      .fetch(endpoint, {
        method: "POST",
        headers: {
          "X-CSRFToken": token,
          "Content-Type": "application/x-www-form-urlencoded",
        },
        body: payload.toString(),
        credentials: "same-origin",
      })
      .catch(function () {
        /* сеть недоступна: выбор остаётся в cookie этого браузера */
      });
  }

  /* Сохраняет выбор темы и применяет его немедленно. */
  function setTheme(theme) {
    const density = currentAppearance().density;
    applyTheme(theme);
    persistAppearance(theme, density);
    syncAppearance(theme, density);
    try {
      window.localStorage.setItem(THEME_KEY, theme);
    } catch (error) {
      /* приватный режим: сохранение недоступно, тема действует до закрытия */
    }
  }

  function setDensity(density) {
    const root = document.documentElement;
    if (density === "compact") {
      root.setAttribute("data-density", "compact");
    } else {
      root.removeAttribute("data-density");
    }
    const theme = currentAppearance().theme;
    persistAppearance(theme, density);
    syncAppearance(theme, density);
  }

  Reservix.setTheme = setTheme;
  Reservix.setDensity = setDensity;
  Reservix.getTheme = function () {
    return currentAppearance().theme;
  };

  /* ======================================================================
     Индикатор фоновой загрузки
     ====================================================================== */

  let loadBar = null;
  let loadTimer = null;

  /* Показывает полосу загрузки под шапкой. */
  function startLoading() {
    if (!loadBar) {
      loadBar = document.querySelector(".load-bar");
      if (!loadBar) return;
    }
    clearTimeout(loadTimer);
    loadTimer = setTimeout(function () {
      loadBar.classList.remove("is-complete");
      loadBar.classList.add("is-active");
    }, 180);
  }

  function stopLoading() {
    clearTimeout(loadTimer);
    if (!loadBar) return;
    loadBar.classList.remove("is-active");
    loadBar.classList.add("is-complete");
    setTimeout(function () {
      loadBar.classList.remove("is-complete");
    }, 300);
  }

  /* ======================================================================
     Интеграция с HTMX
     ====================================================================== */

  /* Подключает обработчики HTMX. */
  function initHtmx() {
    if (!window.htmx) return;

    document.body.addEventListener("htmx:configRequest", function (event) {
      const method = (event.detail.verb || "get").toUpperCase();
      if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
        const token = getCookie("reservix_csrf");
        if (token) {
          event.detail.headers["X-CSRFToken"] = token;
        }
      }
    });

    document.body.addEventListener("htmx:beforeRequest", startLoading);
    document.body.addEventListener("htmx:afterRequest", stopLoading);

    // Ошибка сервера при фоновом запросе иначе осталась бы незамеченной:
    // пользователь нажал кнопку, ничего не произошло, причина неизвестна.
    document.body.addEventListener("htmx:responseError", function (event) {
      const status = event.detail.xhr.status;
      let message = t("error.generic");
      if (status === 403) {
        message = t("error.forbidden");
      } else if (status === 404) {
        message = t("error.notFound");
      } else if (status >= 500) {
        message = t("error.server");
      }
      Reservix.toast(message, "error");
    });

    document.body.addEventListener("htmx:sendError", function () {
      Reservix.toast(t("error.offline"), "error");
    });

    // Сервер может передать сообщение в заголовке ответа: так действие,
    // не перерисовывающее страницу, всё равно подтверждается пользователю.
    document.body.addEventListener("htmx:afterOnLoad", function (event) {
      const xhr = event.detail.xhr;
      const message = xhr.getResponseHeader("X-Reservix-Message");
      if (message) {
        const level = xhr.getResponseHeader("X-Reservix-Level") || "info";
        Reservix.toast(decodeURIComponent(message), level);
      }
    });
  }

  /* ======================================================================
     Инициализация
     ====================================================================== */

  document.addEventListener("DOMContentLoaded", function () {
    initHtmx();

    document.querySelectorAll("[data-theme-option]").forEach(function (button) {
      button.addEventListener("click", function () {
        setTheme(button.dataset.themeOption);
      });
    });

    updateThemeControls(currentAppearance().theme);
  });
})();

/* ===== 02-ui.js ========================================================= */
/* Reservix · Компоненты интерфейса */

(function () {
  "use strict";

  const Reservix = (window.Reservix = window.Reservix || {});

  /* ======================================================================
     Уведомления
     ====================================================================== */

  const TOAST_DURATION = 5000;
  const TOAST_ICONS = {
    success: "check-circle",
    error: "alert-circle",
    warning: "alert-triangle",
    info: "info",
  };

  /* Показывает всплывающее уведомление. */
  function toast(message, level, duration) {
    const stack = document.querySelector(".toast-stack");
    if (!stack) return;

    const type = TOAST_ICONS[level] ? level : "info";
    const element = document.createElement("div");
    element.className = "toast toast--" + type;
    // role="status" вместо "alert": сообщение зачитывается по завершении
    // текущей фразы и не прерывает работу пользователя.
    element.setAttribute("role", "status");

    const body = document.createElement("div");
    body.className = "toast__body";
    // textContent, а не innerHTML: сообщение может содержать данные,
    // введённые пользователем, и не должно интерпретироваться как разметка.
    body.textContent = message;

    const close = document.createElement("button");
    close.className = "toast__close";
    close.type = "button";
    close.setAttribute("aria-label", Reservix.t("toast.close", "Закрыть"));
    close.textContent = "×";
    close.addEventListener("click", function () {
      dismissToast(element);
    });

    element.appendChild(body);
    element.appendChild(close);
    stack.appendChild(element);

    const ttl = duration === undefined ? TOAST_DURATION : duration;
    if (ttl > 0) {
      setTimeout(function () {
        dismissToast(element);
      }, ttl);
    }
  }

  function dismissToast(element) {
    if (!element.parentNode) return;
    element.classList.add("is-leaving");
    setTimeout(function () {
      if (element.parentNode) element.parentNode.removeChild(element);
    }, 250);
  }

  Reservix.toast = toast;

  /* ======================================================================
     Выпадающие меню
     ====================================================================== */

  /* Закрывает все открытые меню, кроме указанного. */
  function closeDropdowns(except) {
    document.querySelectorAll("[data-dropdown-menu]").forEach(function (menu) {
      if (menu === except) return;
      menu.hidden = true;
      const trigger = document.querySelector(
        '[data-dropdown-trigger="' + menu.dataset.dropdownMenu + '"]'
      );
      if (trigger) trigger.setAttribute("aria-expanded", "false");
    });
  }

  function initDropdowns() {
    document.addEventListener("click", function (event) {
      const trigger = event.target.closest("[data-dropdown-trigger]");

      if (trigger) {
        event.preventDefault();
        const id = trigger.dataset.dropdownTrigger;
        const menu = document.querySelector('[data-dropdown-menu="' + id + '"]');
        if (!menu) return;

        const willOpen = menu.hidden;
        closeDropdowns(menu);
        menu.hidden = !willOpen;
        trigger.setAttribute("aria-expanded", String(willOpen));

        if (willOpen) {
          const first = menu.querySelector(
            'a, button, [tabindex]:not([tabindex="-1"])'
          );
          if (first) first.focus();
        }
        return;
      }

      // Щелчок вне меню закрывает его. Щелчок по пункту меню тоже: пункт
      // выполняет переход, и оставлять меню открытым не нужно.
      if (!event.target.closest("[data-dropdown-menu]")) {
        closeDropdowns(null);
      } else if (event.target.closest("a, button")) {
        closeDropdowns(null);
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        const open = document.querySelector("[data-dropdown-menu]:not([hidden])");
        if (open) {
          closeDropdowns(null);
          const trigger = document.querySelector(
            '[data-dropdown-trigger="' + open.dataset.dropdownMenu + '"]'
          );
          if (trigger) trigger.focus();
        }
      }
    });
  }

  /* ======================================================================
     Модальные окна
     ====================================================================== */

  /* Подключает модальные окна на элементе <dialog>. */
  function initModals() {
    document.addEventListener("click", function (event) {
      const opener = event.target.closest("[data-modal-open]");
      if (opener) {
        event.preventDefault();
        const modal = document.getElementById(opener.dataset.modalOpen);
        if (modal && typeof modal.showModal === "function") {
          modal.showModal();
        }
        return;
      }

      const closer = event.target.closest("[data-modal-close]");
      if (closer) {
        event.preventDefault();
        const modal = closer.closest("dialog");
        if (modal) modal.close();
      }
    });

    // Щелчок по затемнённой области закрывает окно. Событие click на
    // <dialog> приходит и от подложки, поэтому проверяются координаты.
    document.addEventListener("click", function (event) {
      if (event.target.tagName !== "DIALOG") return;
      const rect = event.target.getBoundingClientRect();
      const inside =
        rect.top <= event.clientY &&
        event.clientY <= rect.top + rect.height &&
        rect.left <= event.clientX &&
        event.clientX <= rect.left + rect.width;
      if (!inside) event.target.close();
    });
  }

  /* ======================================================================
     Подтверждение необратимых действий
     ====================================================================== */

  /* Требует подтверждения перед отправкой формы, помеченной data-confirm. */
  function initConfirmations() {
    document.addEventListener(
      "submit",
      function (event) {
        const form = event.target;
        const message = form.dataset.confirm;
        if (!message) return;
        if (!window.confirm(message)) {
          event.preventDefault();
          event.stopPropagation();
        }
      },
      true
    );

    document.addEventListener("click", function (event) {
      const link = event.target.closest("a[data-confirm]");
      if (!link) return;
      if (!window.confirm(link.dataset.confirm)) {
        event.preventDefault();
      }
    });
  }

  /* ======================================================================
     Главное меню: перенос непоместившихся пунктов
     Разделов двенадцать, и в строку шапки они не помещаются ни на одной настольной ширине.
     ====================================================================== */

  function initMainNav() {
    const nav = document.querySelector("[data-main-nav]");
    if (!nav) return;

    const list = nav.querySelector(".main-nav__list");
    const more = nav.querySelector("[data-nav-more]");
    const trigger = more && more.querySelector("[data-dropdown-trigger]");
    const menu = more && more.querySelector("[data-dropdown-menu]");
    if (!list || !more || !trigger || !menu) return;

    const items = Array.prototype.slice.call(list.querySelectorAll(".main-nav__item"));
    if (!items.length) return;

    const gap = parseFloat(getComputedStyle(list).columnGap) || 0;

    /* Раскладывает пункты, пока они помещаются, остальные уводит в меню. */
    function layout() {
      // Меню строки нет на узких экранах: там разделы открывает панель,
      // вызываемая кнопкой в шапке, и измерять нечего.
      if (getComputedStyle(nav).display === "none") return;

      // Измерение идёт по полному составу: ширина пункта неизвестна, пока он скрыт, а ширина
      // строки от состава не зависит — меню растягивается на всё свободное место при любом числе
      // видимых пунктов.
      items.forEach(function (item) {
        item.hidden = false;
      });
      more.hidden = false;

      const widths = items.map(function (item) {
        return item.getBoundingClientRect().width;
      });
      const moreWidth = more.getBoundingClientRect().width;
      const available = nav.getBoundingClientRect().width;

      // Полный состав без кнопки «Ещё» — обычный случай широкого окна.
      let total = widths.reduce(function (sum, width) {
        return sum + width + gap;
      }, -gap);

      let visible = items.length;
      if (total > available) {
        // Кнопка «Ещё» занимает место наравне с пунктами и входит в расчёт.
        let used = moreWidth;
        visible = 0;
        for (let i = 0; i < items.length; i += 1) {
          const next = used + widths[i] + gap;
          if (next > available) break;
          used = next;
          visible += 1;
        }
        // Хотя бы один раздел в строке остаётся всегда: меню из одной кнопки
        // «Ещё» не сообщает, что за ней, и выглядит поломкой вёрстки.
        if (visible === 0) visible = 1;
      }

      const hidden = [];
      items.forEach(function (item, index) {
        const isHidden = index >= visible;
        item.hidden = isHidden;
        if (isHidden) hidden.push(item);
      });

      more.hidden = hidden.length === 0;
      if (more.hidden) {
        menu.hidden = true;
        trigger.setAttribute("aria-expanded", "false");
        trigger.removeAttribute("aria-current");
        menu.textContent = "";
        return;
      }

      // Меню собирается заново: состав зависит от ширины окна и меняется при
      // каждом изменении размера.
      menu.textContent = "";
      let holdsCurrent = false;

      hidden.forEach(function (item) {
        const source = item.querySelector(".main-nav__link");
        if (!source) return;
        const link = document.createElement("a");
        link.className = "dropdown__item";
        link.href = source.getAttribute("href");
        link.textContent = source.textContent.trim();
        if (source.getAttribute("aria-current") === "page") {
          link.setAttribute("aria-current", "page");
          holdsCurrent = true;
        }
        menu.appendChild(link);
      });

      if (holdsCurrent) {
        trigger.setAttribute("aria-current", "true");
      } else {
        trigger.removeAttribute("aria-current");
      }
    }

    layout();

    // Пересчёт при изменении ширины.
    if ("ResizeObserver" in window) {
      let frame = 0;
      const observer = new ResizeObserver(function () {
        // Перекладка меняет размеры наблюдаемого элемента и вызвала бы
        // наблюдателя повторно; кадр анимации разрывает эту цепочку.
        if (frame) cancelAnimationFrame(frame);
        frame = requestAnimationFrame(layout);
      });
      observer.observe(nav);
    } else {
      window.addEventListener("resize", layout);
    }

    // Шрифт интерфейса подключается файлом: до его загрузки ширины пунктов
    // измерены по запасному начертанию и отличаются от итоговых.
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(layout);
    }
  }

  /* ======================================================================
     Мобильное меню
     ====================================================================== */

  function initMobileNav() {
    const toggle = document.querySelector("[data-nav-toggle]");
    const nav = document.querySelector("[data-mobile-nav]");
    if (!toggle || !nav) return;

    toggle.addEventListener("click", function () {
      const willOpen = nav.hidden;
      nav.hidden = !willOpen;
      toggle.setAttribute("aria-expanded", String(willOpen));
      // Блокировка прокрутки фона: иначе при прокрутке открытого меню страница под ним уезжает, и
      // после закрытия пользователь оказывается в неожиданном месте.
      document.body.style.overflow = willOpen ? "hidden" : "";
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !nav.hidden) {
        nav.hidden = true;
        toggle.setAttribute("aria-expanded", "false");
        document.body.style.overflow = "";
        toggle.focus();
      }
    });
  }

  /* ======================================================================
     Вкладки
     ====================================================================== */

  /* Подключает переключение вкладок с поддержкой клавиатуры. */
  function initTabs() {
    document.querySelectorAll('[role="tablist"][data-tabs]').forEach(function (list) {
      const tabs = Array.from(list.querySelectorAll('[role="tab"]'));
      if (!tabs.length) return;
      // Отметка о подключении.
      if (list.dataset.bound === "1") return;
      list.dataset.bound = "1";

      function activate(index) {
        tabs.forEach(function (tab, position) {
          const selected = position === index;
          tab.setAttribute("aria-selected", String(selected));
          tab.tabIndex = selected ? 0 : -1;
          const panel = document.getElementById(tab.getAttribute("aria-controls"));
          if (panel) panel.hidden = !selected;
        });
        tabs[index].focus();
      }

      tabs.forEach(function (tab, index) {
        tab.addEventListener("click", function (event) {
          event.preventDefault();
          activate(index);
        });

        tab.addEventListener("keydown", function (event) {
          let next = null;
          if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
          else if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
          else if (event.key === "Home") next = 0;
          else if (event.key === "End") next = tabs.length - 1;

          if (next !== null) {
            event.preventDefault();
            activate(next);
          }
        });
      });
    });
  }

  /* ======================================================================
     Автоматическая отправка формы фильтров
     ====================================================================== */

  /* Отправляет форму при изменении фильтра. */
  function initAutoSubmit() {
    document.querySelectorAll("[data-auto-submit]").forEach(function (form) {
      // См. комментарий в initTabs: форма фильтров переживает подмену результатов, и повторное
      // подключение обработчиков привело бы к нескольким запросам на одно нажатие клавиши.
      if (form.dataset.bound === "1") return;
      form.dataset.bound = "1";

      let timer = null;

      form.addEventListener("change", function (event) {
        if (event.target.type === "text" || event.target.type === "search") return;
        form.requestSubmit();
      });

      form.addEventListener("input", function (event) {
        if (event.target.type !== "text" && event.target.type !== "search") return;
        clearTimeout(timer);
        timer = setTimeout(function () {
          form.requestSubmit();
        }, 400);
      });
    });
  }

  /* ======================================================================
     Инициализация
     ====================================================================== */

  document.addEventListener("DOMContentLoaded", function () {
    initDropdowns();
    initModals();
    initConfirmations();
    initMainNav();
    initMobileNav();
    initTabs();
    initAutoSubmit();

    // Серверные сообщения переносятся в очередь уведомлений: блок с ними
    // отрисован в разметке, чтобы работать без сценариев.
    document.querySelectorAll("[data-server-message]").forEach(function (node) {
      toast(node.textContent.trim(), node.dataset.serverMessage);
      node.remove();
    });
  });

  // Фрагменты, загруженные HTMX, могут содержать вкладки и формы фильтров.
  document.addEventListener("htmx:afterSwap", function () {
    initTabs();
    initAutoSubmit();
  });
})();

/* ===== 03-palette.js ==================================================== */
/* Reservix · Поиск по системе */

(function () {
  "use strict";

  const Reservix = (window.Reservix = window.Reservix || {});

  let dialog = null;
  let input = null;
  let results = null;
  let entries = [];
  let items = [];
  let activeIndex = 0;

  /* Нормализует строку для поиска. */
  function normalize(value) {
    return String(value || "").toLowerCase().replace(/ё/g, "е").trim();
  }

  /* Оценивает соответствие записи запросу. */
  function score(entry, query) {
    const title = normalize(entry.title);
    const group = normalize(entry.group);
    const keywords = normalize(entry.keywords);

    if (title.startsWith(query)) return 100;

    // Поиск по инициалам слов: «кр» находит «Кадровый резерв».
    const initials = title
      .split(/\s+/)
      .map(function (word) {
        return word.charAt(0);
      })
      .join("");
    if (query.length > 1 && initials.startsWith(query)) return 80;

    if (title.includes(query)) return 60;
    if (group.startsWith(query)) return 40;
    if (keywords.includes(query)) return 30;

    return -1;
  }

  /* Подсвечивает совпавший участок подписи. */
  function highlight(target, text, query) {
    const position = query ? normalize(text).indexOf(query) : -1;
    if (position < 0) {
      target.appendChild(document.createTextNode(text));
      return;
    }
    target.appendChild(document.createTextNode(text.slice(0, position)));
    const mark = document.createElement("mark");
    mark.className = "palette__match";
    mark.textContent = text.slice(position, position + query.length);
    target.appendChild(mark);
    target.appendChild(document.createTextNode(text.slice(position + query.length)));
  }

  /* Собирает строку результата. */
  function buildItem(entry, query) {
    const link = document.createElement("a");
    link.className = "palette__item";
    link.href = entry.url;
    link.setAttribute("role", "option");
    link.setAttribute("aria-selected", "false");

    if (entry.icon) {
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("class", "icon palette__icon");
      svg.setAttribute("width", "18");
      svg.setAttribute("height", "18");
      svg.setAttribute("aria-hidden", "true");
      const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
      use.setAttribute("href", entry.icon);
      svg.appendChild(use);
      link.appendChild(svg);
    }

    const body = document.createElement("span");
    body.className = "palette__text";

    const title = document.createElement("span");
    title.className = "palette__title";
    highlight(title, entry.title, query);
    body.appendChild(title);

    if (entry.keywords) {
      const hint = document.createElement("span");
      hint.className = "palette__hint";
      hint.textContent = entry.keywords;
      body.appendChild(hint);
    }

    link.appendChild(body);

    const group = document.createElement("span");
    group.className = "palette__badge";
    group.textContent = entry.group || "";
    link.appendChild(group);

    return link;
  }

  /* Перерисовывает список результатов. */
  function render(query) {
    if (!results) return;

    const normalized = normalize(query);
    let matched;

    if (!normalized) {
      matched = entries.slice(0, 12);
    } else {
      matched = entries
        .map(function (entry) {
          return { entry: entry, rank: score(entry, normalized) };
        })
        .filter(function (item) {
          return item.rank >= 0;
        })
        .sort(function (a, b) {
          return b.rank - a.rank;
        })
        .slice(0, 12)
        .map(function (item) {
          return item.entry;
        });
    }

    results.textContent = "";
    items = [];
    activeIndex = 0;

    if (!matched.length) {
      const empty = document.createElement("p");
      empty.className = "palette__empty";
      empty.textContent = Reservix.t("palette.empty", "Ничего не найдено");
      results.appendChild(empty);
      return;
    }

    let currentGroup = null;
    matched.forEach(function (entry) {
      if (entry.group && entry.group !== currentGroup) {
        currentGroup = entry.group;
        const heading = document.createElement("p");
        heading.className = "palette__group-title";
        heading.textContent = currentGroup;
        results.appendChild(heading);
      }

      const link = buildItem(entry, normalized);
      results.appendChild(link);
      items.push(link);
    });

    select(0);
  }

  /* Выделяет запись по индексу и сообщает о ней программе чтения с экрана. */
  function select(index) {
    if (!items.length) return;
    items.forEach(function (item) {
      item.classList.remove("is-active");
      item.setAttribute("aria-selected", "false");
    });
    activeIndex = (index + items.length) % items.length;
    const current = items[activeIndex];
    current.classList.add("is-active");
    current.setAttribute("aria-selected", "true");
    current.scrollIntoView({ block: "nearest" });
    if (input) input.setAttribute("aria-activedescendant", ensureId(current));
  }

  function ensureId(node) {
    if (!node.id) node.id = "palette-item-" + activeIndex;
    return node.id;
  }

  function open() {
    if (!dialog || dialog.open) return;
    dialog.showModal();
    input.value = "";
    render("");
    input.focus();
  }

  function close() {
    if (dialog && dialog.open) dialog.close();
  }

  Reservix.openPalette = open;

  document.addEventListener("DOMContentLoaded", function () {
    dialog = document.getElementById("command-palette");
    if (!dialog) return;

    input = dialog.querySelector(".palette__input");
    results = dialog.querySelector(".palette__results");

    const source = document.getElementById("palette-entries");
    if (source) {
      try {
        entries = JSON.parse(source.textContent);
      } catch (error) {
        entries = [];
      }
    }

    input.addEventListener("input", function () {
      render(input.value);
    });

    input.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        select(activeIndex + 1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        select(activeIndex - 1);
      } else if (event.key === "Home") {
        event.preventDefault();
        select(0);
      } else if (event.key === "End") {
        event.preventDefault();
        select(items.length - 1);
      } else if (event.key === "Enter") {
        event.preventDefault();
        if (items[activeIndex]) window.location.href = items[activeIndex].href;
      }
    });

    // Наведение мышью переносит выделение: иначе на экране одновременно
    // видны две подсвеченные строки — под курсором и выбранная клавишами.
    results.addEventListener("mousemove", function (event) {
      const link = event.target.closest(".palette__item");
      if (!link) return;
      const index = items.indexOf(link);
      if (index >= 0 && index !== activeIndex) select(index);
    });

    document.addEventListener("keydown", function (event) {
      // Ctrl+K и Cmd+K — общепринятое сочетание для быстрого поиска.
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        if (dialog.open) {
          close();
        } else {
          open();
        }
      }
    });

    document.querySelectorAll("[data-palette-open]").forEach(function (trigger) {
      trigger.addEventListener("click", function (event) {
        event.preventDefault();
        open();
      });
      // Поле поиска в шапке — кнопка, а не настоящий ввод: набирать текст пользователь начинает
      // уже в открытом окне.
      trigger.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          open();
        }
      });
    });
  });
})();

/* ===== 04-portal.js ===================================================== */
/* Reservix · Сценарии публичной части */

(function () {
  "use strict";

  const Reservix = (window.Reservix = window.Reservix || {});

  /* ======================================================================
     Калькулятор индекса готовности
     ====================================================================== */

  /* Читает данные калькулятора из JSON-блока разметки. */
  function readCriteria() {
    const holder = document.getElementById("readiness-data");
    if (!holder) {
      return null;
    }
    try {
      const parsed = JSON.parse(holder.textContent || "[]");
      return Array.isArray(parsed) ? parsed : null;
    } catch (error) {
      return null;
    }
  }

  /* Считает взвешенную сумму нормированных значений критериев. */
  function computeIndex(criteria) {
    let weighted = 0;
    let total = 0;

    criteria.forEach(function (criterion) {
      const weight = Number(criterion.weight) || 0;
      const value = Number(criterion.value) || 0;
      weighted += weight * value;
      total += weight;
    });

    return total > 0 ? weighted / total : 0;
  }

  /* Определяет уровень готовности по значению индекса и порогам страницы. */
  function resolveLevel(index, root) {
    const high = Number(root.dataset.thresholdHigh) || 75;
    const medium = Number(root.dataset.thresholdMedium) || 55;

    if (index >= high) {
      return { label: Reservix.t("readiness.high"), signal: "ready" };
    }
    if (index >= medium) {
      return { label: Reservix.t("readiness.medium"), signal: "develop" };
    }
    return { label: Reservix.t("readiness.low"), signal: "risk" };
  }

  /* Собирает интерактивный калькулятор индекса готовности. */
  function initReadinessCalculator() {
    const root = document.querySelector("[data-readiness-calculator]");
    const criteria = readCriteria();

    if (!root || !criteria || criteria.length === 0) {
      return;
    }

    const fields = root.querySelector("[data-calculator-fields]");
    const valueNode = root.querySelector("[data-calculator-value]");
    const levelNode = root.querySelector("[data-calculator-level]");
    const markerNode = root.querySelector("[data-calculator-marker]");

    if (!fields || !valueNode || !levelNode) {
      return;
    }

    root.hidden = false;

    /* Перерисовывает итог по текущим значениям движков. */
    function refresh() {
      const index = computeIndex(criteria);
      const level = resolveLevel(index, root);

      valueNode.textContent = Reservix.formatNumber(index, 1);
      levelNode.textContent = level.label;
      levelNode.className = "readiness__value readiness__value--" + level.signal;

      // Положение отметки на шкале порогов.
      if (markerNode) {
        const bounded = Math.max(0, Math.min(100, index));
        markerNode.style.setProperty("--readiness-position", bounded + "%");
        markerNode.dataset.signal = level.signal;
      }
    }

    criteria.forEach(function (criterion, position) {
      const row = document.createElement("div");
      row.className = "field";

      const label = document.createElement("label");
      label.className = "field__label";
      label.setAttribute("for", "criterion-" + position);
      label.textContent = criterion.name;

      const share = document.createElement("span");
      share.className = "field__optional";
      share.textContent =
        Reservix.t("readiness.weight", "вес") +
        " " +
        Reservix.formatNumber(Number(criterion.weight) * 100, 1) +
        " %";
      label.appendChild(share);

      const input = document.createElement("input");
      input.type = "range";
      input.min = "0";
      input.max = "100";
      input.step = "1";
      input.value = String(criterion.value);
      input.id = "criterion-" + position;
      input.className = "field-range";
      input.setAttribute("aria-describedby", "criterion-value-" + position);

      const output = document.createElement("output");
      output.className = "field-range__value";
      output.id = "criterion-value-" + position;
      output.setAttribute("for", input.id);
      output.textContent = String(criterion.value);

      /* Доля закрашенной части дорожки задаётся переменной, а не свойством background: правило
         живёт в таблице стилей и подчиняется теме, здесь остаётся только число. */
      function paint() {
        input.style.setProperty("--range-fill", input.value + "%");
      }

      input.addEventListener("input", function () {
        criterion.value = Number(input.value);
        output.textContent = input.value;
        paint();
        refresh();
      });

      paint();

      const track = document.createElement("div");
      track.className = "field-range__row";
      track.appendChild(input);
      track.appendChild(output);

      row.className = "field field--range";
      row.appendChild(label);
      row.appendChild(track);
      fields.appendChild(row);
    });

    refresh();
  }

  /* ======================================================================
     Матрица 9-box на главной странице
     ====================================================================== */

  /* Показывает управленческое решение по выбранной зоне матрицы. */
  function initNineBox() {
    const grid = document.querySelector("[data-ninebox]");
    if (!grid) return;

    const nameNode = document.querySelector("[data-ninebox-name]");
    const textNode = document.querySelector("[data-ninebox-decision]");
    if (!nameNode || !textNode) return;

    const cells = Array.from(grid.querySelectorAll("[data-ninebox-cell]"));
    let pinned = cells.find(function (item) {
      return item.classList.contains("is-selected");
    }) || null;

    /* Показывает описание зоны; выделение остаётся только у выбранной щелчком. */
    function show(cell) {
      // textContent, а не innerHTML: значение приходит из атрибута разметки.
      nameNode.textContent = cell ? cell.dataset.name || "" : "";
      textNode.textContent = cell ? cell.dataset.decision || "" : "";
    }

    function pin(cell) {
      pinned = cell;
      cells.forEach(function (item) {
        const current = item === cell;
        item.classList.toggle("is-selected", current);
        item.setAttribute("aria-pressed", String(current));
      });
      show(cell);
    }

    function restore() {
      if (pinned) show(pinned);
    }

    cells.forEach(function (cell) {
      cell.addEventListener("click", function () {
        pin(cell);
      });
      cell.addEventListener("mouseenter", function () {
        show(cell);
      });
      cell.addEventListener("focus", function () {
        show(cell);
      });
      cell.addEventListener("blur", restore);
    });

    grid.addEventListener("mouseleave", restore);
  }

  /* ======================================================================
     Указатель глоссария
     ====================================================================== */

  /* Подсвечивает букву указателя, соответствующую видимой части словаря. */
  function initGlossaryIndex() {
    const index = document.querySelector("[data-glossary-index]");
    const groups = document.querySelectorAll("[data-glossary-letter]");

    if (!index || groups.length === 0 || !("IntersectionObserver" in window)) {
      return;
    }

    const observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) {
            return;
          }
          const letter = entry.target.dataset.glossaryLetter;
          index.querySelectorAll("a").forEach(function (link) {
            link.setAttribute(
              "aria-current",
              link.dataset.letter === letter ? "true" : "false"
            );
          });
        });
      },
      { rootMargin: "-20% 0px -70% 0px" }
    );

    groups.forEach(function (group) {
      observer.observe(group);
    });
  }

  /* ======================================================================
     Запуск
     ====================================================================== */

  document.addEventListener("DOMContentLoaded", function () {
    initReadinessCalculator();
    initNineBox();
    initGlossaryIndex();
  });
})();
