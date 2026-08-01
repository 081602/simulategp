/* ==========================================================
   SimulateGP — Main JavaScript
   ========================================================== */

'use strict';

/* ----------------------------------------------------------
   1. Game Status Auto-Poll (every 30s)
      Fetches /api/game/status and updates the navbar phase badge
   ---------------------------------------------------------- */
(function initGameStatusPoller() {
  var badge = document.querySelector('.navbar .badge.fs-6');
  if (!badge) return; // admin view or not logged in

  function fetchStatus() {
    fetch('/api/game/status')
      .then(function(res) {
        if (!res.ok) return;
        return res.json();
      })
      .then(function(data) {
        if (!data) return;
        if (data.phase_label) {
          badge.textContent = data.phase_label;
        }
        // Update badge color based on pause state
        if (data.is_paused) {
          badge.classList.remove('bg-warning', 'bg-success');
          badge.classList.add('bg-danger');
        } else {
          badge.classList.remove('bg-danger', 'bg-success');
          badge.classList.add('bg-warning');
        }
      })
      .catch(function() {
        // Silently fail — network error, etc.
      });
  }

  // Poll every 30 seconds
  setInterval(fetchStatus, 30000);
})();


/* ----------------------------------------------------------
   2. Fill Investor Fields Toggle
      Show/hide fill investor options when "willing to fill" checkbox changes
   ---------------------------------------------------------- */
(function initFillInvestorToggle() {
  var chk = document.getElementById('willing_to_fill');
  var fields = document.getElementById('fillInvestorFields');
  if (!chk || !fields) return;

  function toggle() {
    if (chk.checked) {
      fields.classList.remove('d-none');
    } else {
      fields.classList.add('d-none');
      // Clear inputs when hiding
      fields.querySelectorAll('input').forEach(function(inp) {
        inp.value = '';
      });
    }
  }

  chk.addEventListener('change', toggle);
  toggle(); // run on load
})();


/* ----------------------------------------------------------
   3. Syndicate Partner Selector Toggle
      Show/hide syndicate partner checkboxes based on ts_type radio selection
   ---------------------------------------------------------- */
(function initSyndicateToggle() {
  var radios = document.querySelectorAll('input[name="ts_type"]');
  var partnerDiv = document.getElementById('syndicatePartners');
  if (!radios.length || !partnerDiv) return;

  function toggle() {
    var selected = document.querySelector('input[name="ts_type"]:checked');
    if (selected && selected.value === 'syndicate') {
      partnerDiv.classList.remove('d-none');
    } else {
      partnerDiv.classList.add('d-none');
      // Uncheck all partner checkboxes when hiding
      partnerDiv.querySelectorAll('input[type="checkbox"]').forEach(function(cb) {
        cb.checked = false;
      });
    }
  }

  radios.forEach(function(radio) {
    radio.addEventListener('change', toggle);
  });
  toggle(); // run on load
})();


/* ----------------------------------------------------------
   4. Dynamic Team Inputs on Setup Page
      Renders N team input forms based on team_count number input
      (Implemented inline in setup.html for cleaner template scoping,
       but the re-render helper is available globally here)
   ---------------------------------------------------------- */
window.SimulateGP = window.SimulateGP || {};

window.SimulateGP.renderTeamInputs = function(containerId, count) {
  var container = document.getElementById(containerId);
  if (!container) return;
  count = Math.min(Math.max(parseInt(count) || 1, 1), 20);
  container.innerHTML = '';

  for (var i = 1; i <= count; i++) {
    var html = [
      '<div class="card border-secondary mb-3">',
      '  <div class="card-header border-secondary py-2">',
      '    <h6 class="mb-0 text-warning fw-semibold">',
      '      <i class="bi bi-person-circle me-2"></i>Team ' + i,
      '    </h6>',
      '  </div>',
      '  <div class="card-body p-3">',
      '    <div class="row g-3">',
      '      <div class="col-md-4">',
      '        <label class="form-label small fw-semibold">Firm Name</label>',
      '        <input type="text" class="form-control form-control-sm bg-dark text-white border-secondary"',
      '               name="team_' + i + '_name" placeholder="e.g. Atlas Capital" maxlength="100" required>',
      '      </div>',
      '      <div class="col-md-4">',
      '        <label class="form-label small fw-semibold">Username</label>',
      '        <input type="text" class="form-control form-control-sm bg-dark text-white border-secondary"',
      '               name="team_' + i + '_username" placeholder="e.g. team' + i + '" maxlength="50" required>',
      '      </div>',
      '      <div class="col-md-4">',
      '        <label class="form-label small fw-semibold">Password</label>',
      '        <input type="text" class="form-control form-control-sm bg-dark text-white border-secondary"',
      '               name="team_' + i + '_password" placeholder="Password" maxlength="100"',
      '               required autocomplete="new-password">',
      '      </div>',
      '    </div>',
      '  </div>',
      '</div>'
    ].join('');
    var wrapper = document.createElement('div');
    wrapper.innerHTML = html;
    container.appendChild(wrapper.firstChild);
  }
};


/* ----------------------------------------------------------
   5. Confirmation Dialogs for Irreversible Actions
      Any element with class "confirm-action" and data-confirm attribute
      will show a confirm dialog before proceeding.
      Also handles form submissions for the crank page.
   ---------------------------------------------------------- */
(function initConfirmActions() {
  document.addEventListener('click', function(e) {
    var el = e.target.closest('.confirm-action');
    if (!el) return;

    var message = el.getAttribute('data-confirm') ||
                  'Are you sure? This action cannot be undone.';

    if (!window.confirm(message)) {
      e.preventDefault();
      e.stopPropagation();
      return false;
    }
  });
})();


/* ----------------------------------------------------------
   6. IRR Display Formatting
      Formats elements with class "irr-display" as percentages with color coding
   ---------------------------------------------------------- */
(function initIrrDisplay() {
  document.querySelectorAll('.irr-display').forEach(function(el) {
    var val = parseFloat(el.textContent);
    if (isNaN(val)) return;

    // Apply color class based on value
    el.classList.remove('text-success', 'text-danger', 'text-warning', 'text-info');
    if (val >= 20) {
      el.classList.add('text-success');
    } else if (val >= 10) {
      el.classList.add('text-info');
    } else if (val >= 0) {
      el.classList.add('text-warning');
    } else {
      el.classList.add('text-danger');
    }
  });
})();


/* ----------------------------------------------------------
   7. Debt Fields Toggle
      Show/hide debt amount & interest rate based on include_debt checkbox
   ---------------------------------------------------------- */
(function initDebtToggle() {
  var includeChk = document.getElementById('include_debt');
  var debtFields = document.getElementById('debtFields');
  if (!includeChk || !debtFields) return;

  var debtInputs = debtFields.querySelectorAll('input');

  function toggle() {
    if (includeChk.checked) {
      debtInputs.forEach(function(inp) { inp.disabled = false; });
      debtFields.style.opacity = '1';
    } else {
      debtInputs.forEach(function(inp) {
        inp.disabled = true;
        inp.value = '';
      });
      debtFields.style.opacity = '0.4';
    }
  }

  includeChk.addEventListener('change', toggle);
  toggle(); // run on load
})();


/* ----------------------------------------------------------
   8. Fill Investor Row Enable/Disable
      On finalize_deal page: enable fill amount input when include checkbox is ticked
   ---------------------------------------------------------- */
(function initFillRowToggle() {
  document.querySelectorAll('.fill-include-check').forEach(function(chk) {
    var row = chk.closest('tr');
    if (!row) return;
    var amtInput = row.querySelector('.fill-amount-input');
    if (!amtInput) return;

    function toggle() {
      amtInput.disabled = !chk.checked;
      if (!chk.checked) amtInput.value = '';
    }

    chk.addEventListener('change', toggle);
    toggle(); // run on load
  });
})();


/* ----------------------------------------------------------
   9. Crank Form — Require Confirmation Checkbox
      Prevent crank form submission if confirm checkbox not ticked
   ---------------------------------------------------------- */
(function initCrankFormGuard() {
  ['phase1CrankForm', 'phase2CrankForm'].forEach(function(formId) {
    var form = document.getElementById(formId);
    if (!form) return;

    form.addEventListener('submit', function(e) {
      var confirmChk = form.querySelector('input[name="confirm"]');
      if (confirmChk && !confirmChk.checked) {
        e.preventDefault();
        alert('You must check the confirmation box before running the crank.');
        return false;
      }
      // Secondary JS confirm dialog
      var btn = form.querySelector('[type="submit"]');
      var msg = btn ? (btn.getAttribute('data-confirm') || '') : '';
      if (msg && !window.confirm(msg)) {
        e.preventDefault();
        return false;
      }
    });
  });
})();


/* ----------------------------------------------------------
   10. Pre-Money Valuation Warning (finalize_deal)
       Warn inline when final value drops below 90% of original
   ---------------------------------------------------------- */
(function initValuationWarning() {
  var finalPreMoney = document.getElementById('final_pre_money');
  var warnEl = document.getElementById('valuationWarning');
  if (!finalPreMoney || !warnEl) return;

  // Read the min allowed from data attribute if present, else from template-set value
  var minAttr = finalPreMoney.getAttribute('data-min-allowed');
  var minAllowed = minAttr ? parseFloat(minAttr) : null;
  if (!minAllowed) return;

  finalPreMoney.addEventListener('input', function() {
    var val = parseFloat(this.value);
    if (isNaN(val)) return;

    if (val < minAllowed) {
      warnEl.classList.remove('text-info', 'text-secondary');
      warnEl.classList.add('text-danger', 'fw-semibold');
      warnEl.querySelector('i') && (
        warnEl.querySelector('i').className = 'bi bi-exclamation-triangle-fill text-danger me-1'
      );
    } else {
      warnEl.classList.remove('text-danger', 'fw-semibold');
      warnEl.classList.add('text-info');
      warnEl.querySelector('i') && (
        warnEl.querySelector('i').className = 'bi bi-info-circle text-info me-1'
      );
    }
  });
})();


/* ----------------------------------------------------------
   11. Sortable table columns
      Any <th class="sortable"> toggles asc/desc sorting of its
      column on click. Cells may provide data-sort for a clean
      sort key; otherwise their text content is used.
   ---------------------------------------------------------- */
(function initSortableTables() {
  document.querySelectorAll('table').forEach(function (table) {
    var headers = table.querySelectorAll('th.sortable');
    if (!headers.length) return;
    headers.forEach(function (th) {
      th.addEventListener('click', function () {
        var idx = th.cellIndex;
        var tbody = table.querySelector('tbody');
        if (!tbody) return;
        var dir = th.dataset.sortDir === 'asc' ? 'desc' : 'asc';
        headers.forEach(function (h) { delete h.dataset.sortDir; });
        th.dataset.sortDir = dir;
        var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
        rows.sort(function (a, b) {
          function key(row) {
            var cell = row.cells[idx];
            if (!cell) return '';
            var v = cell.dataset.sort !== undefined ? cell.dataset.sort : cell.textContent;
            return v.trim().toLowerCase();
          }
          var av = key(a), bv = key(b);
          if (av < bv) return dir === 'asc' ? -1 : 1;
          if (av > bv) return dir === 'asc' ? 1 : -1;
          return 0;
        });
        rows.forEach(function (r) { tbody.appendChild(r); });
      });
    });
  });
})();


/* ----------------------------------------------------------
   12. Toast Notifications helper (for future use)
   ---------------------------------------------------------- */
window.SimulateGP.showToast = function(message, type) {
  type = type || 'info';
  var container = document.getElementById('toastContainer');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toastContainer';
    container.className = 'toast-container position-fixed bottom-0 end-0 p-3';
    container.style.zIndex = '1090';
    document.body.appendChild(container);
  }

  var bgClass = {
    success: 'bg-success',
    danger: 'bg-danger',
    warning: 'bg-warning text-dark',
    info: 'bg-info text-dark'
  }[type] || 'bg-secondary';

  var toastEl = document.createElement('div');
  toastEl.className = 'toast align-items-center text-white border-0 show ' + bgClass;
  toastEl.setAttribute('role', 'alert');
  toastEl.innerHTML = [
    '<div class="d-flex">',
    '  <div class="toast-body">' + message + '</div>',
    '  <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>',
    '</div>'
  ].join('');

  container.appendChild(toastEl);

  // Auto-remove after 4 seconds
  setTimeout(function() {
    toastEl.remove();
  }, 4000);
};
