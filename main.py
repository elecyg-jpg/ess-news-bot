import os
import requests
from bs4 import BeautifulSoup
import feedparser
import urllib.parse
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

def get_original_url_and_text(rss_link):
    """구글 우회 링크를 뚫고 진짜 원문 링크와 본문 텍스트를 가져오는 함수"""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        # 리디렉션 추적을 허용하여 최종 원본 기사 링크 확보
        response = requests.get(rss_link, headers=headers, timeout=10, allow_redirects=True)
        final_url = response.url
        
        # 기사 본문 스크래핑 (기자 이름, 이메일, 승인일 등 추출용)
        soup = BeautifulSoup(response.content, 'html.parser')
        full_text = soup.get_text(separator=' ', strip=True)
        return final_url, full_text[:3000] # AI가 분석하기 충분하도록 초반 3000자만 추출
    except Exception as e:
        return rss_link, "본문 수집 실패"

def get_news_and_summarize():
    try:
        feed = feedparser.parse(rss_url)
        if not feed.entries:
            send_telegram_message("👀 현재 시간 기준으로 새로운 뉴스 기사가 없습니다.")
            return

        news_texts = []
        for i, entry in enumerate(feed.entries[:5]):
            title = entry.title
            rss_link = entry.link
            source = entry.source.title if 'source' in entry else "신문사 미상"
            
            # 직접 원본 사이트 접속 및 데이터 추출
            final_link, article_body = get_original_url_and_text(rss_link)
            
            news_texts.append(
                f"[기사 데이터 {i+1}]\n"
                f"제목: {title}\n"
                f"신문사: {source}\n"
                f"최종원본링크: {final_link}\n"
                f"기사본문텍스트: {article_body}"
            )

        combined_news = "\n\n".join(news_texts)

        # AI에게 보고서 양식 및 데이터 추출 엄격 지시
        prompt = f"""
        당신은 전력망 및 에너지저장장치(ESS) 산업의 전문 분석가입니다. 
        아래 제공된 '기사본문텍스트'를 면밀히 분석하여, 상사에게 즉시 보고할 수 있는 [정확한 출력 양식]으로 브리핑을 작성해주세요.
        
        [정확한 출력 양식]
        1. [{{기사제목}}] ({{본문에서 찾은 승인일 또는 발행일}} / {{신문사명}} / {{본문에서 찾은 기자명}} / {{본문에서 찾은 기자이메일}})
        ㅇ (기사의 핵심 주제 1줄 요약)
          - (세부 내용 1)
          - (세부 내용 2)
          - {{최종원본링크}}
        
        2. [{{기사제목}}] ... (이하 반복)

        [데이터 추출 및 작성 주의사항]
        - 발행일/승인일: 기사본문텍스트 안에서 '승인 2026.07.15', '입력일' 등의 날짜를 찾아 기재하세요. (없을 경우에만 '날짜 미상' 처리)
        - 기자명/이메일: 본문 끝부분이나 초반에 있는 'ㅇㅇㅇ 기자', 'ㅇㅇㅇ기자', '@' 기호가 포함된 이메일 주소를 반드시 찾아내어 기재하세요. (없을 경우 빈칸 처리하지 말고 '기자 미상' / '이메일 미상' 기재)
        - 내용 요약: 줄글을 절대 피하고, 반드시 위 양식의 글머리기호(ㅇ, -)와 들여쓰기를 지켜 가독성을 극대화하세요.
        - 링크: 마지막 줄에는 반드시 오류 없이 접속 가능한 {{최종원본링크}}를 원문 그대로 기재하세요.

        [수집된 기사 데이터]
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
