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
