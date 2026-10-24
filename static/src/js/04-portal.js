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
