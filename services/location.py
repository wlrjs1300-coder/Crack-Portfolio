def validate_member_location(address, latitude, longitude, region_city='', region_district=''):
    """회원 주소와 거리 계산용 좌표를 검증하고 정규화한다."""
    address = address.strip() if isinstance(address, str) else ''
    region_city = region_city.strip() if isinstance(region_city, str) else ''
    region_district = region_district.strip() if isinstance(region_district, str) else ''

    if not address or len(address) > 255:
        return None, '주소 검색을 통해 거주지 주소를 입력해주세요.'
    if len(region_city) > 50 or len(region_district) > 50:
        return None, '주소의 지역 정보가 올바르지 않습니다.'

    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return None, '주소 위치를 확인하지 못했습니다. 주소를 다시 검색해주세요.'

    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None, '주소 좌표가 올바르지 않습니다.'

    return {
        'address': address,
        'latitude': latitude,
        'longitude': longitude,
        'region_city': region_city,
        'region_district': region_district,
    }, None
