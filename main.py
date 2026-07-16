import os
import requests
import feedparser
import urllib.parse
from bs4 import BeautifulSoup
import google.generativeai as genai

# 환경변수 설정
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-3.5-flash')

keywords = '("배전망 ESS" OR "배전망 에너지저장장치" OR "AI 활용 ESS" OR "VPP" OR "가상발전소" OR "재생에너지 계통연계" OR "접속대기" OR "계통혼잡" OR "출력제어" OR "동적운영한계" OR "Dynamic Operating Envelope" OR "BESS")'
encoded_query = urllib.parse.quote(keywords)
rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"

def fetch_article_data(google_url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        
        # 1차 접속: 구글 뉴스 리디렉션 방어막 페이지
        response = requests.get(google_url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.content, 'html.parser')
        actual_url = response.url
        
        # 방어막 페이지에서 실제 신문사 링크(URL) 찾아내기
        refresh_tag = soup.find('meta', attrs={'http-equiv': 'refresh'})
        if refresh_tag:
            content = refresh_tag.get('content', '')
            if 'url=' in content.lower():
                actual_url = content.split('url=')[-1].strip("'\" ")
                
                # 2차 접속: 찾아낸 진짜 신문사 링크로 재접속!
                response = requests.get(actual_url, headers=headers, timeout=15)
                soup = BeautifulSoup(response.content, 'html.parser')

        # AI를 돕기 위해 파이썬이 1차로 메타데이터(기자, 날짜) 직접 추출
        ext_author = ""
        ext_date = ""
        for meta in soup.find_all('meta'):
            name = meta.get('name', '').lower()
            prop = meta.get('property', '').lower()
            content = meta.get('content', '')
            
            if name in ['author', 'byl', 'article:author'] or prop in ['author', 'article:author']:
                if content: ext_author = content
            if name in ['pubdate', 'date', 'article:published_time'] or prop in ['article:published_time']:
                if content: ext_date = content

        # 불순물 제거 및 기사 본문 추출
        for script in soup(["script", "style", "header", "footer", "nav"]):
            script.extract()
        text = soup.get_text(separator=' ', strip=True)
        
        return actual_url, ext_author, ext_date, text
    except:
        return google_url, "", "", ""

def get_news_and_summarize():
    try:
        feed = feedparser.parse(rss_url)
        if not feed.entries:
            send_telegram_message("👀 현재 시간 기준으로 새로운 뉴스 기사가 없습니다.")
            return

        news_texts = []
        for i, entry in enumerate(feed.entries[:5]):
            title = entry.title
            google_link = entry.link
            source = entry.source.title if 'source' in entry else "신문사 미상"
            
            # 강화된 크롤러 함수 호출
            actual_link, ext_author, ext_date, article_text = fetch_article_data(google_link)
            
            news_texts.append(f"[기사 {i+1}]\n제목: {title}\n신문사: {source}\n원본링크: {actual_link}\n파이썬추출_기자: {ext_author}\n파이썬추출_날짜: {ext_date}\n기사본문: {article_text}")

        combined_news = "\n\n".join(news_texts)

        # 3. 이중 데이터로 정확도를 높인 프롬프트
        prompt = f"""
        당신은 전력망 및 에너지저장장치(ESS) 산업의 전문 분석가입니다. 
        아래 제공된 [기사 데이터]를 분석하여 부서장에게 보고할 최고 수준의 브리핑을 작성해주세요.
        
        [정확한 출력 양식]
        1. [{{기사제목}}]
        ㅇ {{기자명}} 기자 | {{기자이메일}} | 승인 {{승인일(YYYY.MM.DD HH:MM 형식)}} | {{신문사명}}
        ㅇ (기사의 핵심 주제 1줄 요약)
          - (세부 내용 1)
          - (세부 내용 2)
          - 기사 원문 링크: {{원본링크}}
        
        [데이터 추출 절대 규칙]
        - 기사 데이터에 제공된 '파이썬추출_기자'와 '파이썬추출_날짜' 정보가 있다면 최우선으로 양식에 채워 넣으세요.
        - 파이썬 추출 정보가 부족하다면, '기사본문' 텍스트를 정밀하게 뒤져서 기자의 실명, 이메일 주소, 날짜를 반드시 찾아내세요.
        - 링크는 구글 링크가 아닌 반드시 정제된 '원본링크'를 그대로 사용하세요.

        [기사 데이터]
        {combined_news}
        """
        
        response = model.generate_content(prompt)
        summary = response.text

        final_message = f"⚡ [배전망/ESS 주요 뉴스 브리핑]\n\n{summary}"
        send_telegram_message(final_message)

    except Exception as e:
        send_telegram_message(f"🚨 뉴스 요약 중 오류가 발생했습니다: {e}")

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    requests.post(url, data=payload)

if __name__ == "__main__":
    get_news_and_summarize()
