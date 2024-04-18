__all__ = ()

import os
import uuid
from random import randint

from flask import Blueprint, redirect, render_template, url_for, request, abort, jsonify
from flask_login import LoginManager, login_user, current_user, logout_user

from Sketchy.database import Sketch
from database import User, Session
from forms import LoginForm, SketchCreate
from settings import TEMPLATES_PATH, MEDIA_PATH, ALLOWED_MEDIA_EXTENSIONS, UPLOAD_PATH
from utils import lazy_loader, get_session, coincidence
import datetime
from PIL import Image

blueprint = Blueprint(
    name='views',
    import_name=__name__,
    template_folder=TEMPLATES_PATH
)
login_manager = LoginManager()


@lazy_loader
@login_manager.user_loader
def load_user(uid):
    return Session().query(User).get(uid)


@blueprint.route('/')
def index():
    is_system_call = request.args.get('rule') is not None and request.args.get('query') is not None

    if not is_system_call:
        previews = [
            url_for('static', filename=f'img/{filename}')
            for filename in os.listdir(MEDIA_PATH) if filename.startswith('preview-sketch')
        ]  # gets previews filenames and then convert them to relative path (from static)

        return render_template('index.html', previews=previews)

    limit = request.args.get('limit', 0, type=int)
    offset = request.args.get('offset', 0, type=int)
    rule = request.args.get('rule', 'any')
    query = request.args.get('query')

    session = Session()

    matching = []
    for entry in session.query(Sketch).all():
        if rule == 'author':
            values = (entry.author.username, entry.author.login)
        elif rule == 'place':
            values = (entry.place,)
        elif rule == 'title':
            values = (entry.name,)
        elif rule == 'any':
            values = (entry.name, entry.place, entry.author.username, entry.author.login)
        else:
            return abort(404)

        mc = 0
        match = False
        for value in values:
            c = coincidence(value, query)
            if c > mc:
                mc = c
            if query in value:
                match = True

        if mc >= 0.7 or match is True and entry not in map(lambda x: x[0], matching):
            matching.append((entry, mc))

    matching.sort(key=lambda m: m[1], reverse=True)
    return jsonify(status=200, data={'results_left': max(len(matching) - offset - limit, 0)},
                   rendered='\n'.join(render_template('sketch-preview.html', sketch=match[0])
                                      for match in matching[offset:offset + limit]))


@blueprint.route('/sketch')
def sketch():
    sid = request.args.get('sid')

    if sid is None:
        sid = randint(1, Session().query(Sketch).count())
        return redirect(f'/sketch?{sid=}')

    return render_template('sketch.html')  # render template here


@blueprint.route('/auth', methods=['GET', 'POST'])
def auth():
    if current_user.is_authenticated:
        return redirect('/profile')

    form = LoginForm(new=bool(request.args.get('n')))

    if (user := form.validate_on_submit()) is False:  # validation failed or form just created
        return render_template('signin-form.html' if request.args.get('n') is None else 'signup-form.html', form=form)

    if user is None:
        session = Session()
        user = User()
        user.login = form.login.data
        user.password = form.password.data
        session.add(user)
        session.commit()
    login_user(user, remember=True)

    return redirect(request.args.get('referrer', '/profile'))


@blueprint.route('/logout')
def logout():
    logout_user()  # this will ignore non-authenticated users
    return redirect('/')


@blueprint.route('/profile', methods=['GET', 'POST'])
def profile():
    if request.args.get('uid', current_user.is_authenticated) is False:
        return redirect('/auth')  # non-authenticated user tries to check their account, redirect to auth

    user = load_user(request.args.get('uid', getattr(current_user, 'id', None)))
    if not user.sketches:
        session = Session()
        for i in range(100):
            sk = Sketch()
            sk.name = f'sketch_{i}'
            sk.image = f'../preview-sketch-{i % 3 + 1}.jpg'
            sk.place = 'Москва, Красная площадь'
            sk.author_id = user.id
            session.add(sk)
        session.commit()
    if user is None:
        return abort(404)  # request provided invalid uid

    if request.method == 'GET':
        is_system_call = request.args.get('limit') is not None and request.args.get('offset') is not None
        limit = request.args.get('limit', 0, type=int)
        offset = request.args.get('offset', 0, type=int)
        view = request.args.get('view', 'sketches')
        if not is_system_call or getattr(user, view, None) is None:
            # passing sketches_num, followers_num and follows_num as render_template params
            # since jinja cannot access these attributes with getattr(user, ...) due to lazy_loader
            return render_template('profile.html', user=user, sketches_num=len(user.sketches),
                                   followers_num=len(user.followers), follows_num=len(user.follows))
        results_left = max(len(getattr(user, view)) - offset - limit, 0)
        return jsonify(status=200, data={'results_left': results_left}, rendered='\n'.join(render_template(
            'sketch-preview.html' if view == 'sketches' else 'user-preview.html',
            sketch=item, user=item, author_context=False
        ) for item in getattr(user, view)[offset:offset + limit]))

    errors = {}
    params = request.form
    attachments = dict((field.split('-')[0], files) for field, files in request.files.items())

    for param, value in params.items():
        if param == 'username':
            if not value:
                errors[param] = 'Отображаемое имя не указано'
                continue
            user.username = value
        if param == 'description':
            user.description = value
        if param == 'image':
            if (attachment := attachments.get(param)) is None:
                errors[param] = 'Изображение не выбрано'
                continue
            tp = attachment.content_type.split('/')[-1]
            if tp.upper() not in ALLOWED_MEDIA_EXTENSIONS:
                errors[param] = 'Неподдерживаемый тип файла'
                continue
            # remove previous image if exists and not default
            default = str(User.image.default)
            default = default[default.index("'") + 1:default.replace("'", '', 1).index("'") + 1]
            if os.path.isfile(os.path.join(UPLOAD_PATH, user.image)) and user.image != default:
                os.remove(os.path.join(UPLOAD_PATH, user.image))
            # generate unique filename
            while (image_name := f'{uuid.uuid4()}.{tp.lower()}') in os.listdir(UPLOAD_PATH):
                pass
            user.image = image_name
            attachment.save(os.path.join(UPLOAD_PATH, user.image))  # save filename to uploads folder
        if param == 'followers':
            # merge current_user to user's session to not access current_user in different threads
            merged = get_session(user).merge(current_user)
            if value == 'false' and current_user not in user.followers:
                user.followers.append(merged)
            elif value == 'true' and current_user in user.followers:
                user.followers.remove(merged)
            else:
                errors[param] = 'Не удалось синхронизировать данные с сервером'

    if errors:
        return jsonify(status=400, errors=errors, rendered=render_template(
            'response-message.html', status=400, description='\n'.join(errors[error] for error in errors)
        ))

    # creating response json before commit because it will close user's session
    ret = jsonify(
        status=200, user_data={'avatar': user.image}, rendered=render_template(
            'response-message.html', status=200, description='Изменения профиля сохранены'
        )
    )
    get_session(user).commit()
    return ret


@blueprint.route('/sketch_create', methods=['GET', 'POST'])
def sketch_create():
    if not current_user.is_authenticated:
        return redirect('/auth')
    form = SketchCreate()
    session = Session()
    user_load = load_user(request.args.get('uid', getattr(current_user, 'id', None)))
    if (user := form.validate_on_submit()) is False:  # validation failed or form just created
        return render_template('form.html', form=form)
    sk = Sketch()

    sk.name = form.name.data
    sk.place = form.place.data
    sk.author_id = user_load.id
    image_name = form.image
    print()
    tp = 'png'
    while (image_name := f'{uuid.uuid4()}.{tp}') in os.listdir(UPLOAD_PATH):
        pass
    sk.image_name = image_name
    sk.time_created = datetime.datetime.now()
    session.add(sk)
    session.commit()
    return redirect(request.args.get('referrer', '/profile'))
