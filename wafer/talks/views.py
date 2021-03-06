from django.core.exceptions import PermissionDenied, ValidationError
from django.core.urlresolvers import reverse_lazy
from django.http import HttpResponseRedirect
from django.views.generic import DetailView
from django.views.generic.edit import CreateView, UpdateView, DeleteView
from django.views.generic.list import ListView
from django.conf import settings
from django.db.models import Q
from django.http import Http404

from reversion import revisions
from rest_framework import viewsets
from rest_framework.permissions import (
    DjangoModelPermissions, DjangoModelPermissionsOrAnonReadOnly,
    BasePermission)
from rest_framework_extensions.mixins import NestedViewSetMixin

from wafer.utils import LoginRequiredMixin
from wafer.talks.models import Talk, TalkType, TalkUrl, ACCEPTED, CANCELLED
from wafer.talks.forms import get_talk_form_class
from wafer.talks.serializers import TalkSerializer, TalkUrlSerializer
from wafer.users.models import UserProfile
from wafer.utils import order_results_by


class EditOwnTalksMixin(object):
    '''Users can edit their own talks as long as the talk is
       "Under Consideration"'''
    def get_object(self, *args, **kwargs):
        object_ = super(EditOwnTalksMixin, self).get_object(*args, **kwargs)
        if object_.can_edit(self.request.user):
            return object_
        else:
            raise PermissionDenied


class UsersTalks(ListView):
    template_name = 'wafer.talks/talks.html'
    paginate_by = 25

    @order_results_by('talk_id')
    def get_queryset(self):
        # self.request will be None when we come here via the static site
        # renderer
        if (self.request and Talk.can_view_all(self.request.user)):
            return Talk.objects.all()
        return Talk.objects.filter(Q(status=ACCEPTED) |
                                   Q(status=CANCELLED))


class TalkView(DetailView):
    template_name = 'wafer.talks/talk.html'
    model = Talk

    def get_object(self, *args, **kwargs):
        '''Only talk owners can see talks, unless they've been accepted'''
        object_ = super(TalkView, self).get_object(*args, **kwargs)
        if object_.can_view(self.request.user):
            return object_
        else:
            raise PermissionDenied

    def get_context_data(self, **kwargs):
        context = super(TalkView, self).get_context_data(**kwargs)
        context['can_edit'] = self.object.can_edit(self.request.user)
        return context


class TalkCreate(LoginRequiredMixin, CreateView):
    model = Talk
    template_name = 'wafer.talks/talk_form.html'

    def get_form_class(self):
        return get_talk_form_class()

    def get_form_kwargs(self):
        kwargs = super(TalkCreate, self).get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super(TalkCreate, self).get_context_data(**kwargs)
        can_submit = getattr(settings, 'WAFER_TALKS_OPEN', True)
        if can_submit and TalkType.objects.exists():
            # Check for all talk types being disabled
            can_submit = TalkType.objects.filter(
                disable_submission=False).count() > 0
        context['can_submit'] = can_submit
        return context

    @revisions.create_revision()
    def form_valid(self, form):
        if not getattr(settings, 'WAFER_TALKS_OPEN', True):
            # Should this be SuspiciousOperation?
            raise ValidationError("Talk submission isn't open")
        # Eaaargh we have to do the work of CreateView if we want to set values
        # before saving
        self.object = form.save(commit=False)
        self.object.corresponding_author = self.request.user
        self.object.save()
        revisions.set_user(self.request.user)
        revisions.set_comment("Talk Created")
        # Save the author information as well (many-to-many fun)
        form.save_m2m()
        return HttpResponseRedirect(self.get_success_url())


class TalkUpdate(EditOwnTalksMixin, UpdateView):
    model = Talk
    template_name = 'wafer.talks/talk_form.html'

    def get_form_class(self):
        return get_talk_form_class()

    def get_form_kwargs(self):
        kwargs = super(TalkUpdate, self).get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super(TalkUpdate, self).get_context_data(**kwargs)
        context['can_edit'] = self.object.can_edit(self.request.user)
        return context

    @revisions.create_revision()
    def form_valid(self, form):
        revisions.set_user(self.request.user)
        revisions.set_comment("Talk Modified")
        return super(TalkUpdate, self).form_valid(form)


class TalkDelete(EditOwnTalksMixin, DeleteView):
    model = Talk
    template_name = 'wafer.talks/talk_delete.html'
    success_url = reverse_lazy('wafer_page')

    @revisions.create_revision()
    def form_valid(self, form):
        # We don't add any metadata, as the admin site
        # doesn't show it for deleted talks.
        return super(TalkDelete, self).form_valid(form)


class Speakers(ListView):
    model = Talk
    template_name = 'wafer.talks/speakers.html'

    def _by_row(self, speakers, n):
        return [speakers[i:i + n] for i in range(0, len(speakers), n)]

    def get_context_data(self, **kwargs):
        context = super(Speakers, self).get_context_data(**kwargs)
        speakers = UserProfile.objects.filter(
            user__talks__status='A').distinct().prefetch_related(
                'user').order_by('user__first_name', 'user__last_name')
        context["speaker_rows"] = self._by_row(speakers, 4)
        return context


class TalksViewSet(viewsets.ModelViewSet, NestedViewSetMixin):
    """API endpoint that allows talks to be viewed or edited."""
    queryset = Talk.objects.none()  # Needed for the REST Permissions
    serializer_class = TalkSerializer
    # XXX: Do we want to allow authors to edit talks via the API?
    permission_classes = (DjangoModelPermissionsOrAnonReadOnly, )

    @order_results_by('talk_id')
    def get_queryset(self):
        # We override the default implementation to only show accepted talks
        # to people who aren't part of the management group
        if self.request.user.id is None:
            # Anonymous user, so just accepted or cancelled talks
            return Talk.objects.filter(Q(status=ACCEPTED) |
                                       Q(status=CANCELLED))
        elif Talk.can_view_all(self.request.user):
            return Talk.objects.all()
        else:
            # Also include talks owned by the user
            # XXX: Should this be all authors rather than just
            # the corresponding author?
            return Talk.objects.filter(
                Q(status=ACCEPTED) |
                Q(status=CANCELLED) |
                Q(corresponding_author=self.request.user))


class TalkExistsPermission(BasePermission):
    def has_permission(self, request, view):
        talk_id = view.get_parents_query_dict()['talk']
        if not Talk.objects.filter(pk=talk_id).exists():
            raise Http404
        return True


class TalkUrlsViewSet(viewsets.ModelViewSet, NestedViewSetMixin):
    """API endpoint that allows talks to be viewed or edited."""
    queryset = TalkUrl.objects.all().order_by('id')
    serializer_class = TalkUrlSerializer
    permission_classes = (DjangoModelPermissions, TalkExistsPermission)

    def create(self, request, *args, **kw):
        request.data['talk'] = self.get_parents_query_dict()['talk']
        return super(TalkUrlsViewSet, self).create(request, *args, **kw)

    def update(self, request, *args, **kw):
        request.data['talk'] = self.get_parents_query_dict()['talk']
        return super(TalkUrlsViewSet, self).update(request, *args, **kw)
