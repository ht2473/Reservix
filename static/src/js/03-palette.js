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
