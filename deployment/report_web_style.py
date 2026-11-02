"""Shared responsive report reading styles; PNG typography remains unchanged."""
READER_STYLE = r"""<style>
body{font-size:18px;width:100%}main,main.page{width:min(1000px,100%);padding:24px;margin:auto}h1{font-size:56px}h2{font-size:30px}
.meta,.muted{font-size:16px}.stats{grid-template-columns:repeat(4,minmax(0,1fr))}.stats small{font-size:14px}.stats b{font-size:30px}
.lede,.summary,.topic li,blockquote p{font-size:18px}nav a{font-size:18px}.topic h3{font-size:24px}.topic h3 span{font-size:18px}
blockquote footer,.sources p{font-size:16px}details,.sources b,.sources small{font-size:14px}
.topic-time{font-size:14px;line-height:1.5;color:#647084;margin:8px 0 14px}
.rank li{grid-template-columns:40px 48px minmax(0,1fr) 80px;font-size:18px}.rank li b{grid-column:auto;font-size:18px}
.site-home{max-width:1000px;margin:0 auto;padding:16px 24px;font:16px system-ui}
nav>ol{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 40px;margin:18px 0 0;padding-inline-start:28px}
nav>ol>li{min-width:0;margin:0;padding-left:4px}nav>ol>li::marker{color:#66758b;font-variant-numeric:tabular-nums}
nav>ol>li>a{display:block;min-height:44px;padding:6px 10px;line-height:1.6;text-decoration:none;overflow-wrap:anywhere;border-radius:8px}
nav>ol>li>a:hover{background:#f0f2f7;color:#263f68}nav>ol>li>a:focus-visible{outline:2px solid #5978ab;outline-offset:2px}
@media(max-width:760px){nav>ol{grid-template-columns:minmax(0,1fr);gap:10px;margin-top:16px;padding-inline-start:26px}}
@media(max-width:760px){main,main.page{padding:16px}h1{font-size:40px}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.rank li{grid-template-columns:30px 40px minmax(0,1fr) 60px;gap:8px}.rank .avatar{width:36px;height:36px}.topic,blockquote,nav{padding:16px}.topic ul{padding-left:22px}.leader-grid{grid-template-columns:1fr}}
</style>"""

def reader_styles(renderer=None):
    css = READER_STYLE
    if renderer is not None:
        css += '<style>' + renderer.RHYTHM_CHART_CSS + renderer.SOURCE_MESSAGES_CSS + '</style>'
    return css
