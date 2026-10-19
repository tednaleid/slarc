// ABOUTME: Shows an export's times in the viewer's time zone and rebuilds its day dividers to match.
// ABOUTME: With --tz the server formatted every time, so only the jump-to-date control is wired up.
(function () {
  "use strict";

  function dayKey(date) {
    var month = String(date.getMonth() + 1).padStart(2, "0");
    var day = String(date.getDate()).padStart(2, "0");
    return date.getFullYear() + "-" + month + "-" + day;
  }

  function localizeTimes() {
    var clock = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
    var full = new Intl.DateTimeFormat(undefined, { dateStyle: "full", timeStyle: "short" });
    document.querySelectorAll("time[data-format]").forEach(function (element) {
      var date = new Date(element.getAttribute("datetime"));
      var text = full.format(date);
      var format = element.dataset.format;
      if (format === "time") {
        element.textContent = clock.format(date);
        var author = element.dataset.authorTime;
        element.title = author ? text + " (" + author + ")" : text;
      } else if (format === "edited") {
        element.title = "Edited " + text;
      } else if (format === "full") {
        element.textContent = text;
      }
    });
  }

  function rebuildDayDividers() {
    var label = new Intl.DateTimeFormat(undefined, {
      weekday: "long",
      year: "numeric",
      month: "long",
      day: "numeric",
    });
    document.querySelectorAll(".messages").forEach(function (list) {
      list.querySelectorAll(":scope > .day-divider").forEach(function (divider) {
        divider.remove();
      });
      var last = null;
      Array.from(list.children).forEach(function (child) {
        if (!child.dataset.ts) {
          return;
        }
        var date = new Date(parseFloat(child.dataset.ts) * 1000);
        var key = dayKey(date);
        if (key === last) {
          return;
        }
        last = key;
        var divider = document.createElement("div");
        divider.className = "day-divider";
        divider.dataset.day = key;
        var span = document.createElement("span");
        span.textContent = label.format(date);
        divider.appendChild(span);
        list.insertBefore(divider, child);
      });
    });
  }

  function wireJumpToDate() {
    document.querySelectorAll("section.conversation").forEach(function (section) {
      var input = section.querySelector(".jump input");
      if (!input) {
        return;
      }
      input.addEventListener("change", function () {
        var dividers = Array.from(section.querySelectorAll(".day-divider"));
        var target = dividers.find(function (divider) {
          return divider.dataset.day >= input.value;
        });
        (target || dividers[dividers.length - 1] || section).scrollIntoView();
      });
    });
  }

  if (!document.body.dataset.tz) {
    localizeTimes();
    rebuildDayDividers();
  }
  wireJumpToDate();
})();
