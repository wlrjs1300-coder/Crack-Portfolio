"""운영 비밀번호를 소스나 명령 기록에 남기지 않는 관리자 계정 CLI."""

import re

import click
from flask.cli import with_appcontext
from werkzeug.security import generate_password_hash

from extensions import db
from models import Member


USERNAME_PATTERN = re.compile(r'^[A-Za-z0-9_]{4,30}$')
EMAIL_PATTERN = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')


def init_admin_cli(app):
    @app.cli.command('create-admin')
    @click.option('--username', prompt='관리자 아이디')
    @click.option('--email', prompt='관리자 이메일')
    @click.option('--nickname', prompt='관리자 표시 이름', default='관리자')
    @click.password_option(prompt='관리자 비밀번호', confirmation_prompt=True)
    @with_appcontext
    def create_admin(username, email, nickname, password):
        """대화형 입력으로 최초 관리자 계정을 생성합니다."""
        username = username.strip()
        email = email.strip().lower()
        nickname = nickname.strip()
        if not USERNAME_PATTERN.fullmatch(username):
            raise click.ClickException('아이디는 영문, 숫자, 밑줄 조합 4~30자여야 합니다.')
        if not EMAIL_PATTERN.fullmatch(email) or len(email) > 120:
            raise click.ClickException('이메일 형식이 올바르지 않습니다.')
        if not nickname or len(nickname) > 20:
            raise click.ClickException('표시 이름은 1~20자여야 합니다.')
        if len(password) < 10 or len(password) > 128:
            raise click.ClickException('비밀번호는 10~128자여야 합니다.')
        if Member.query.filter_by(username=username).first():
            raise click.ClickException('이미 사용 중인 아이디입니다.')
        if Member.query.filter_by(email=email).first():
            raise click.ClickException('이미 사용 중인 이메일입니다.')

        member = Member(
            username=username,
            email=email,
            nickname=nickname,
            password_hash=generate_password_hash(password),
            role='admin',
            is_admin=True,
            active=True,
            points=0,
        )
        db.session.add(member)
        db.session.commit()
        click.echo('관리자 계정을 생성했습니다.')
