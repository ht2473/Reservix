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
