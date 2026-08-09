/**
 * Виджет amoCRM «SBAvito — переписки ботов» (этап 14.7, кирпич 3).
 *
 * Показывает менеджеру ленту диалогов ИИ-ботов из нашего бэкенда: пункт в левом
 * меню открывает страницу со списком чатов (GET /api/chats), клик по чату —
 * полную переписку (GET /api/dialog/{id}). Адрес API и токен задаются в
 * настройках виджета (manifest → settings), в код не зашиты.
 *
 * Родная лента чатов amoCRM показывает только сообщения клиента (реплики бота
 * как исходящие в кастомный канал amojo не выходят — тупик 14.3b), поэтому
 * полную переписку клиент+бот даёт этот виджет из нашей БД.
 */
define(['jquery'], function ($) {
  var CustomWidget = function () {
    var self = this;

    function settings() {
      var s = self.get_settings() || {};
      return {
        base: (s.api_base || '').replace(/\/+$/, ''),
        token: s.api_token || ''
      };
    }

    function i18n(key) {
      return self.i18n('panel')[key] || key;
    }

    function esc(t) {
      return $('<div>').text(t == null ? '' : String(t)).html();
    }

    // Запрос к нашему API с bearer-токеном. Возвращает промис с JSON.
    function api(path) {
      var cfg = settings();
      return $.ajax({
        url: cfg.base + path,
        method: 'GET',
        headers: { Authorization: 'Bearer ' + cfg.token },
        dataType: 'json'
      });
    }

    function $area() {
      // Рабочая область левого меню виджета.
      return $('#work-area-' + self.get_settings().widget_code + ', .widget-work-area')
        .first();
    }

    function renderChats($root) {
      $root.html('<div class="sbavito-loading">' + esc(i18n('loading')) + '</div>');
      api('/api/chats').done(function (data) {
        var chats = (data && data.chats) || [];
        if (!chats.length) {
          $root.html('<div class="sbavito-empty">' + esc(i18n('empty')) + '</div>');
          return;
        }
        var html = '<ul class="sbavito-chats">';
        chats.forEach(function (c) {
          var who = c.client_name || c.client_username || c.chat_key;
          html += '<li class="sbavito-chat" data-id="' + esc(c.dialog_id) + '">'
            + '<span class="sbavito-acc">' + esc(c.account) + '</span> '
            + '<b>' + esc(who) + '</b>'
            + '<div class="sbavito-preview">' + esc(c.preview || '') + '</div>'
            + '<div class="sbavito-time">' + esc(c.last_message_at || '') + '</div>'
            + '</li>';
        });
        html += '</ul>';
        $root.html(html);
        $root.find('.sbavito-chat').on('click', function () {
          renderDialog($root, $(this).data('id'));
        });
      }).fail(function () {
        $root.html('<div class="sbavito-error">' + esc(i18n('error')) + '</div>');
      });
    }

    function renderDialog($root, dialogId) {
      $root.html('<div class="sbavito-loading">' + esc(i18n('loading')) + '</div>');
      api('/api/dialog/' + encodeURIComponent(dialogId)).done(function (data) {
        var msgs = (data && data.messages) || [];
        var html = '<a href="#" class="sbavito-back">' + esc(i18n('back')) + '</a>'
          + '<div class="sbavito-thread">';
        msgs.forEach(function (m) {
          var kto = m.role === 'bot' ? i18n('bot') : i18n('client');
          html += '<div class="sbavito-msg sbavito-' + esc(m.role) + '">'
            + '<div class="sbavito-role">' + esc(kto) + '</div>'
            + '<div class="sbavito-body">' + esc(m.body) + '</div>'
            + '<div class="sbavito-time">' + esc(m.created_at || '') + '</div>'
            + '</div>';
        });
        html += '</div>';
        $root.html(html);
        $root.find('.sbavito-back').on('click', function (e) {
          e.preventDefault();
          renderChats($root);
        });
      }).fail(function () {
        $root.html('<div class="sbavito-error">' + esc(i18n('error')) + '</div>');
      });
    }

    function renderPanel() {
      var $root = $area();
      if (!$root.length) { return; }
      var cfg = settings();
      if (!cfg.base || !cfg.token) {
        $root.html('<div class="sbavito-empty">' + esc(i18n('no_settings')) + '</div>');
        return;
      }
      renderChats($root);
    }

    this.callbacks = {
      // Форма настроек (адрес API + токен) рисуется amoCRM из manifest.settings.
      settings: function () {},
      onSave: function () { return true; },
      init: function () { return true; },
      bind_actions: function () { return true; },
      // Обычный render (карточки/списки) нам не нужен — работаем на своей странице.
      render: function () { return true; },
      // Открытие страницы виджета из левого меню.
      advancedSettings: function () {
        renderPanel();
        return true;
      },
      // Некоторые сборки amoCRM зовут loadPreloadedData/renderLeftMenu — держим
      // renderPanel идемпотентным, повторный вызов просто перерисует список.
      destroy: function () {}
    };
  };
  return CustomWidget;
});
