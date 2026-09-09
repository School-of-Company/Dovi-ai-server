from app.context.gradle_dependency_diff import extract_dependency_changes

# 실제 build.gradle.kts 스타일 diff. 버전 bump(gson), 신규 의존성 추가(jjwt-api),
# classifier가 붙은 좌표(querydsl-jpa:...:jakarta), 버전이 없는 선언(postgresql,
# Spring Boot BOM 관리 대상)을 함께 커버한다.
_REAL_PATCH_SAMPLE = """\
@@ -30,10 +30,13 @@
 dependencies {
     // Spring Starters
     implementation("org.springframework.boot:spring-boot-starter-web")
     runtimeOnly("org.postgresql:postgresql")
     // JSON & Validation
-    implementation("com.google.code.gson:gson:2.8.9")
+    implementation("com.google.code.gson:gson:2.13.1")
+    // Security (JWT)
+    implementation("io.jsonwebtoken:jjwt-api:0.11.5")
     // QueryDSL
     implementation("com.querydsl:querydsl-jpa:5.0.0:jakarta")
     annotationProcessor("com.querydsl:querydsl-apt:5.0.0:jakarta")
"""


def test_extracts_version_bump() -> None:
    changes = extract_dependency_changes(_REAL_PATCH_SAMPLE)
    gson = next(c for c in changes if c.name == "com.google.code.gson:gson")
    assert gson.version == "2.13.1"
    assert gson.new_file_line == 35
    assert gson.evidence_line == '+    implementation("com.google.code.gson:gson:2.13.1")'


def test_extracts_newly_added_dependency() -> None:
    changes = extract_dependency_changes(_REAL_PATCH_SAMPLE)
    jjwt = next(c for c in changes if c.name == "io.jsonwebtoken:jjwt-api")
    assert jjwt.version == "0.11.5"
    assert jjwt.new_file_line == 37


def test_ignores_unchanged_context_lines() -> None:
    # querydsl-jpa는 이 patch에서 컨텍스트로만 등장한다(안 바뀜) — 잡히면 안 된다.
    changes = extract_dependency_changes(_REAL_PATCH_SAMPLE)
    assert not any(c.name == "com.querydsl:querydsl-jpa" for c in changes)


def test_ignores_dependency_without_explicit_version() -> None:
    # postgresql은 Spring Boot BOM이 버전을 관리해 좌표 문자열에 버전이 없다 —
    # GAV 패턴(group:artifact:version)에 안 맞으므로 안 잡혀야 한다.
    changes = extract_dependency_changes(_REAL_PATCH_SAMPLE)
    assert not any("postgresql" in c.name for c in changes)


def test_ignores_classifier_suffixed_coordinate_when_unchanged() -> None:
    changes = extract_dependency_changes(_REAL_PATCH_SAMPLE)
    assert not any(c.name == "com.querydsl:querydsl-apt" for c in changes)


def test_returns_empty_list_for_non_gradle_text() -> None:
    assert extract_dependency_changes("just some random text\nno diff here") == []


def test_returns_empty_list_for_empty_patch() -> None:
    assert extract_dependency_changes("") == []


def test_supports_groovy_single_quote_style() -> None:
    patch = """\
@@ -1,2 +1,2 @@
 dependencies {
-    implementation 'com.squareup.retrofit2:retrofit:2.9.0'
+    implementation 'com.squareup.retrofit2:retrofit:2.11.0'
"""
    changes = extract_dependency_changes(patch)
    retrofit = next(c for c in changes if c.name == "com.squareup.retrofit2:retrofit")
    assert retrofit.version == "2.11.0"
