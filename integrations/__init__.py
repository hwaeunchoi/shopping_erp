"""integrations 패키지: 쇼핑몰/광고 플랫폼 플러그인 커넥터.

malls/, ads/에 공통 인터페이스(BaseMallConnector/BaseAdConnector)와 플랫폼별
더미 구현체가 있다. 동적 로딩은 integrations.malls.get_mall_connector() /
integrations.ads.get_ad_connector()를 사용한다.

carriers/(택배사 연동)는 아직 향후 확장 범위로, 이 패키지의 대상이 아니다.
"""
