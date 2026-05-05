// 결과 페이지 공유 카드 — 카톡/X/스레드/인스타/링크복사 동작.
//
// 채널별 동작 매트릭스:
//   X·스레드           : 데스크톱·모바일 모두 공식 인텐트 URL 새 창 (intent/tweet, intent/post)
//   카카오톡 / 인스타   : 모바일은 시스템 공유 시트(Web Share API + 이미지 파일),
//                        데스크톱은 링크 복사. 메타·카카오 모두 데스크톱 웹용 공식 인텐트
//                        URL을 제공하지 않으므로 동일 처리.
//   링크 복사          : 항상 클립보드 복사
//
// OG 이미지는 카톡·X·스레드가 URL에서 자동으로 가져온다. 인스타는 OG 미리보기를
// 거의 렌더하지 않으므로 모바일 공유 시트에서 이미지 파일을 함께 전달한다.
(function () {
  'use strict';

  var card = document.querySelector('.share-card');
  if (!card) return;

  var shareUrl   = card.dataset.shareUrl;
  var shareTitle = card.dataset.shareTitle;
  var ogUrl      = card.dataset.ogUrl;
  var toastEl    = document.getElementById('shareToast');

  function track(channel) {
    try {
      if (window.bpa && typeof window.bpa.capture === 'function') {
        window.bpa.capture('share_card_click', { channel: channel });
      } else if (window.posthog && typeof window.posthog.capture === 'function') {
        window.posthog.capture('share_card_click', { channel: channel });
      }
    } catch (_) { /* analytics 실패는 무시 */ }
  }

  var toastTimer = null;
  function showToast(msg) {
    if (!toastEl) return;
    toastEl.textContent = msg;
    toastEl.classList.add('is-visible');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      toastEl.classList.remove('is-visible');
    }, 2400);
  }

  function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    // execCommand 폴백 — iOS Safari 구버전 등
    return new Promise(function (resolve, reject) {
      try {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        var ok = document.execCommand('copy');
        document.body.removeChild(ta);
        ok ? resolve() : reject(new Error('copy failed'));
      } catch (e) { reject(e); }
    });
  }

  function copyLink(toastMsg) {
    copyToClipboard(shareUrl)
      .then(function () { showToast(toastMsg || '링크를 복사했어요.'); })
      .catch(function () { showToast('링크 복사 실패. 주소창에서 직접 복사해 주세요.'); });
  }

  function openPopup(url) {
    var w = 600, h = 600;
    var left = (window.screen.width - w) / 2;
    var top  = (window.screen.height - h) / 2;
    var win = window.open(url, '_blank', 'width=' + w + ',height=' + h + ',left=' + left + ',top=' + top);
    if (!win) window.location.href = url;
  }

  function isMobile() {
    return /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
  }

  // 모바일 시스템 공유 시트 — Web Share API Level 2 (파일 포함).
  // OG 이미지를 fetch해 File로 만들어 navigator.share에 넘긴다.
  // 인스타·카톡·라인 등이 시트의 옵션으로 등장하고, 사용자가 앱을 선택하면 이미지+텍스트가
  // 그대로 해당 앱으로 전달된다. 사용자가 시트를 취소하면 AbortError가 떨어지는데,
  // 이 경우 결과 페이지에 그대로 머물도록 폴백하지 않는다.
  function shareViaSystemSheet() {
    return fetch(ogUrl)
      .then(function (r) { return r.ok ? r.blob() : Promise.reject(new Error('og fetch')); })
      .then(function (blob) {
        var file = new File([blob], 'bpa-share.png', { type: 'image/png' });
        if (!navigator.canShare || !navigator.canShare({ files: [file] })) {
          return Promise.reject(new Error('canShare files unavailable'));
        }
        return navigator.share({
          files: [file],
          title: shareTitle,
          text: shareTitle + '\n' + shareUrl,
        });
      });
  }

  function canSystemShare() {
    return isMobile() && typeof fetch === 'function' && navigator.canShare && navigator.share;
  }

  // 카카오톡·인스타 공통 핸들러 — 모바일은 시스템 시트, 데스크톱은 링크 복사.
  // 메타·카카오 모두 데스크톱 웹용 공식 공유 인텐트 URL을 제공하지 않아 동일 처리.
  function shareViaSheetOrCopy(toastMsg) {
    if (canSystemShare()) {
      shareViaSystemSheet().catch(function (err) {
        // 사용자가 시트를 취소하면 AbortError — 결과 페이지에 머문다 (아무 동작 안 함)
        if (err && err.name === 'AbortError') return;
        // 그 외 실패(파일 공유 미지원 등)는 링크 복사로 폴백
        copyLink(toastMsg);
      });
      return;
    }
    copyLink(toastMsg);
  }

  function shareKakao() {
    shareViaSheetOrCopy('링크를 복사했어요. 카카오톡 채팅창에 붙여넣어 주세요.');
  }

  function shareInstagram() {
    shareViaSheetOrCopy('링크를 복사했어요. 인스타 프로필/스토리에 붙여넣어 주세요.');
  }

  function shareX() {
    var url = 'https://twitter.com/intent/tweet'
      + '?text=' + encodeURIComponent(shareTitle)
      + '&url='  + encodeURIComponent(shareUrl);
    openPopup(url);
  }

  function shareThreads() {
    var text = shareTitle + '\n' + shareUrl;
    var url = 'https://www.threads.net/intent/post?text=' + encodeURIComponent(text);
    openPopup(url);
  }

  function shareCopy() {
    copyLink('링크를 복사했어요.');
  }

  card.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-share]');
    if (!btn) return;
    var channel = btn.dataset.share;
    track(channel);
    switch (channel) {
      case 'kakao':     shareKakao(); break;
      case 'x':         shareX(); break;
      case 'threads':   shareThreads(); break;
      case 'instagram': shareInstagram(); break;
      case 'copy':      shareCopy(); break;
    }
  });
})();
