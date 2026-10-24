/*!
 * Reservix · Начальный сценарий
 *
 * Выполняется до первой отрисовки: снимает класс no-js и применяет
 * сохранённую тему. Подключается отдельно, в бандл app.js не входит.
 */

(function () {
  "use strict";

  var root = document.documentElement;

  /* Снятие класса no-js. */
  root.className = root.className.replace(/\bno-js\b/, "js");

  /* Подстраховка по теме оформления. */
  if (!root.hasAttribute("data-theme")) {
    try {
      var stored = window.localStorage.getItem("reservix:theme");
      if (stored === "light" || stored === "dark") {
        root.setAttribute("data-theme", stored);
      }
    } catch (error) {
      /* приватный режим: хранилище недоступно, остаётся системная тема */
    }
  }
})();
