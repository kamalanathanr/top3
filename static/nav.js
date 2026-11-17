/**
 * Shared top navigation bar for all Top3 pages.
 *
 * Single source of truth for the site's menu — add/remove/reorder links
 * here and every page picks up the change automatically. Pages just need:
 *   <div id="site-nav"></div>
 *   <script src="/static/nav.js"></script>
 * placed where the old <nav> block used to live, plus the existing
 * `.nav-link { @apply text-gray-700 hover:text-blue-600 font-medium transition; }`
 * rule in their own <style> block.
 */
(function () {
    const NAV_LINKS = [
        { href: '/', label: 'Home' },
        { href: '/profile-page', label: 'Profile' },
        { href: '/news', label: 'News' },
        { href: '/investment-overview', label: 'Market' },
        { href: '/planning', label: 'Planning' },
        { href: '/cashflow', label: 'Cash Flow' },
        { href: '/bonds', label: 'Bonds' },
        { href: '/trips', label: 'Trips' },
        { href: '/experiences', label: 'Experiences' },
        { href: '/templates-page', label: 'Templates' },
        { href: '/results-page', label: 'Logs' },
    ];

    function renderNav() {
        const path = window.location.pathname;

        const linksHtml = NAV_LINKS.map(function (link) {
            const isActive = link.href === path;
            const cls = isActive ? 'nav-link text-blue-600 font-semibold' : 'nav-link';
            return '<a href="' + link.href + '" class="' + cls + '">' + link.label + '</a>';
        }).join('');

        const navHtml =
            '<nav class="fixed top-0 left-0 right-0 bg-white border-b border-gray-200 shadow-sm z-50">' +
                '<div class="max-w-7xl mx-auto px-6 py-4 flex items-center gap-8">' +
                    '<h1 class="text-2xl font-bold text-blue-600">Discover</h1>' +
                    '<div class="hidden md:flex gap-8">' + linksHtml + '</div>' +
                '</div>' +
            '</nav>';

        const mount = document.getElementById('site-nav');
        if (mount) {
            mount.outerHTML = navHtml;
        } else {
            document.body.insertAdjacentHTML('afterbegin', navHtml);
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', renderNav);
    } else {
        renderNav();
    }
})();
