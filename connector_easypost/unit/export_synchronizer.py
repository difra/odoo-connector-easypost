# -*- coding: utf-8 -*-
# © 2016 LasLabs Inc.
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).

import logging
import dateutil.parser
import pytz

from contextlib import contextmanager

import psycopg2

import openerp
from openerp.tools.translate import _
from openerp.addons.connector.queue.job import job, related_action
from openerp.addons.connector.unit.synchronizer import Exporter
from openerp.addons.connector.exception import (IDMissingInBackend,
                                                RetryableJobError,
                                                )
from .import_synchronizer import import_record
from ..connector import get_environment
from ..related_action import unwrap_binding

_logger = logging.getLogger(__name__)


"""
Exporters for Easypost.
In addition to its export job, an exporter has to:
* check in Easypost if the record has been updated more recently than the
  last sync date and if yes, delay an import
* call the ``bind`` method of the binder to update the last sync date
"""


class EasypostBaseExporter(Exporter):
    """ Base exporter for Easypost """

    def __init__(self, connector_env):
        """
        :param connector_env: current environment (backend, session, ...)
        :type connector_env: :class:`connector.connector.ConnectorEnvironment`
        """
        super(EasypostBaseExporter, self).__init__(connector_env)
        self.binding_id = None
        self.easypost_id = None

    def _delay_import(self):
        """ Schedule an import of the record.
        Adapt in the sub-classes when the model is not imported
        using ``import_record``.
        """
        # force is True because the sync_date will be more recent
        # so the import would be skipped
        assert self.easypost_id
        import_record.delay(self.session, self.model._name,
                            self.backend_record.id, self.easypost_id,
                            force=True)

    def _should_import(self):
        """ Before the export, compare the update date
        in Easypost and the last sync date in Odoo,
        if the former is more recent, schedule an import
        to not miss changes done in Easypost.
        """
        assert self.binding_record
        if not self.easypost_id:
            return False
        sync = self.binding_record.sync_date
        if not sync:
            return True
        record = self.backend_adapter.read(self.easypost_id)
        if not record.updated_at:
            # If empty, the record is immutable. Return not changed
            return True
        sync_date = openerp.fields.Datetime.from_string(sync)
        sync_date = pytz.utc.localize(sync_date)
        easypost_date = dateutil.parser.parse(record.updated_at)
        return sync_date < easypost_date

    def _get_odoo_data(self):
        """ Return the raw Odoo data for ``self.binding_id`` """
        return self.model.browse(self.binding_id)

    def run(self, binding_id, *args, **kwargs):
        """ Run the synchronization
        :param binding_id: identifier of the binding record to export
        """
        self.binding_id = binding_id
        self.binding_record = self._get_odoo_data()

        self.easypost_id = self.binder.to_backend(self.binding_id)
        try:
            should_import = self._should_import()
        except IDMissingInBackend:
            self.easypost_id = None
            should_import = False
        if should_import:
            self._delay_import()

        result = self._run(*args, **kwargs)

        self.binding_record = self.binder.bind(
            self.easypost_id, self.binding_id,
        )
        # Commit so we keep the external ID when there are several
        # exports (due to dependencies) and one of them fails.
        # The commit will also release the lock acquired on the binding
        # record
        self.session.commit()

        self._after_export()
        return result

    def _run(self):
        """ Flow of the synchronization, implemented in inherited classes"""
        raise NotImplementedError

    def _after_export(self):
        """ Can do several actions after exporting a record on easypost """
        pass


class EasypostExporter(EasypostBaseExporter):
    """ A common flow for the exports to Easypost """

    def __init__(self, connector_env):
        """
        :param connector_env: current environment (backend, session, ...)
        :type connector_env: :class:`connector.connector.ConnectorEnvironment`
        """
        super(EasypostExporter, self).__init__(connector_env)
        self.binding_record = None

    def _lock(self):
        """ Lock the binding record.
        Lock the binding record so we are sure that only one export
        job is running for this record if concurrent jobs have to export the
        same record.
        When concurrent jobs try to export the same record, the first one
        will lock and proceed, the others will fail to lock and will be
        retried later.
        This behavior works also when the export becomes multilevel
        with :meth:`_export_dependencies`. Each level will set its own lock
        on the binding record it has to export.
        """
        sql = ("SELECT id FROM %s WHERE ID = %%s FOR UPDATE NOWAIT" %
               self.model._table)
        try:
            self.session.cr.execute(sql, (self.binding_id, ),
                                    log_exceptions=False)
        except psycopg2.OperationalError:
            _logger.info('A concurrent job is already exporting the same '
                         'record (%s with id %s). Job delayed later.',
                         self.model._name, self.binding_id)
            raise RetryableJobError(
                'A concurrent job is already exporting the same record '
                '(%s with id %s). The job will be retried later.' %
                (self.model._name, self.binding_id))

    def _has_to_skip(self):
        """ Return True if the export can be skipped """
        return False

    @contextmanager
    def _retry_unique_violation(self):
        """ Context manager: catch Unique constraint error and retry the
        job later.
        When we execute several jobs workers concurrently, it happens
        that 2 jobs are creating the same record at the same time (binding
        record created by :meth:`_export_dependency`), resulting in:
            IntegrityError: duplicate key value violates unique
            constraint "easypost_product_product_odoo_uniq"
            DETAIL:  Key (backend_id, odoo_id)=(1, 4851) already exists.
        In that case, we'll retry the import just later.
        .. warning:: The unique constraint must be created on the
                     binding record to prevent 2 bindings to be created
                     for the same Easypost record.
        """
        try:
            yield
        except psycopg2.IntegrityError as err:
            if err.pgcode == psycopg2.errorcodes.UNIQUE_VIOLATION:
                raise RetryableJobError(
                    'A database error caused the failure of the job:\n'
                    '%s\n\n'
                    'Likely due to 2 concurrent jobs wanting to create '
                    'the same record. The job will be retried later.' % err)
            else:
                raise

    def _export_dependency(self, relation, binding_model, exporter_class=None,
                           binding_field='easypost_bind_ids',
                           binding_extra_vals=None):
        """ Export a dependency. The exporter class is a subclass of
        ``EasypostExporter``. If a more precise class need to be defined,
        it can be passed to the ``exporter_class`` keyword argument.
        .. warning:: a commit is done at the end of the export of each
                     dependency. The reason for that is that we pushed a record
                     on the backend and we absolutely have to keep its ID.
                     So you *must* take care not to modify the Odoo
                     database during an export, excepted when writing
                     back the external ID or eventually to store
                     external data that we have to keep on this side.
                     You should call this method only at the beginning
                     of the exporter synchronization,
                     in :meth:`~._export_dependencies`.
        :param relation: record to export if not already exported
        :type relation: :py:class:`odoo.models.BaseModel`
        :param binding_model: name of the binding model for the relation
        :type binding_model: str | unicode
        :param exporter_cls: :py:class:`odoo.addons.connector\
                                        .connector.ConnectorUnit`
                             class or parent class to use for the export.
                             By default: EasypostExporter
        :type exporter_cls: :py:class:`odoo.addons.connector\
                                       .connector.MetaConnectorUnit`
        :param binding_field: name of the one2many field on a normal
                              record that points to the binding record
                              (default: easypost_bind_ids).
                              It is used only when the relation is not
                              a binding but is a normal record.
        :type binding_field: str | unicode
        :binding_extra_vals:  In case we want to create a new binding
                              pass extra values for this binding
        :type binding_extra_vals: dict
        """
        if not relation:
            return
        if exporter_class is None:
            exporter_class = EasypostExporter
        rel_binder = self.binder_for(binding_model)
        # wrap is typically True if the relation is for instance a
        # 'product.product' record but the binding model is
        # 'easypost.easypost.address'
        wrap = relation._model._name != binding_model

        if wrap and hasattr(relation, binding_field):
            domain = [('odoo_id', '=', relation.id),
                      ('backend_id', '=', self.backend_record.id)]
            binding = self.env[binding_model].search(domain)
            if binding:
                assert len(binding) == 1, (
                    'only 1 binding for a backend is '
                    'supported in _export_dependency')
            # we are working with a unwrapped record (e.g.
            # product.category) and the binding does not exist yet.
            # Example: I created a product.product and its binding
            # easypost.easypost.address and we are exporting it, but we need to
            # create the binding for the product.category on which it
            # depends.
            else:
                bind_values = {'backend_id': self.backend_record.id,
                               'odoo_id': relation.id}
                if binding_extra_vals:
                    bind_values.update(binding_extra_vals)
                # If 2 jobs create it at the same time, retry
                # one later. A unique constraint (backend_id,
                # odoo_id) should exist on the binding model
                with self._retry_unique_violation():
                    binding = (self.env[binding_model]
                               .with_context(connector_no_export=True)
                               .sudo()
                               .create(bind_values))
                    # Eager commit to avoid having 2 jobs
                    # exporting at the same time. The constraint
                    # will pop if an other job already created
                    # the same binding. It will be caught and
                    # raise a RetryableJobError.
                    self.session.commit()
        else:
            # If easypost_bind_ids does not exist we are typically in a
            # "direct" binding (the binding record is the same record).
            # If wrap is True, relation is already a binding record.
            binding = relation

        if not rel_binder.to_backend(binding, wrap=False):
            exporter = self.unit_for(exporter_class, model=binding_model)
            exporter.run(binding.id)

    def _export_dependencies(self):
        """ Export the dependencies for the record"""
        return

    def _map_data(self):
        """ Returns an instance of
        :py:class:`~odoo.addons.connector.unit.mapper.MapRecord`
        """
        return self.mapper.map_record(self.binding_record)

    def _validate_data(self, data):
        """ Check if the values to import are correct
        Kept for retro-compatibility. To remove in 8.0
        Pro-actively check before the ``Model.create`` or ``Model.update``
        if some fields are missing or invalid
        Raise `InvalidDataError`
        """
        _logger.warning('Deprecated: _validate_data is deprecated '
                        'in favor of validate_create_data() '
                        'and validate_update_data()')
        self._validate_create_data(data)
        self._validate_update_data(data)

    def _validate_create_data(self, data):
        """ Check if the values to import are correct
        Pro-actively check before the ``Model.create`` if some fields
        are missing or invalid
        Raise `InvalidDataError`
        """
        return

    def _validate_update_data(self, data):
        """ Check if the values to import are correct
        Pro-actively check before the ``Model.update`` if some fields
        are missing or invalid
        Raise `InvalidDataError`
        """
        return

    def _create_data(self, map_record, fields=None, **kwargs):
        """ Get the data to pass to :py:meth:`_create` """
        return map_record.values(for_create=True, fields=fields, **kwargs)

    def _create(self, data):
        """ Create the Easypost record """
        # special check on data before export
        self._validate_create_data(data)
        self.easypost_record = self.backend_adapter.create(data)
        return self.easypost_record

    def _update_data(self, map_record, fields=None, **kwargs):
        """ Get the data to pass to :py:meth:`_update` """
        return map_record.values(fields=fields, **kwargs)

    def _update(self, data):
        """ Update an Easypost record """
        assert self.easypost_id
        # special check on data before export
        self._validate_update_data(data)
        self.easypost_record = self.backend_adapter.update(
            self.easypost_id, data,
        )
        return self.easypost_record

    def _run(self, fields=None):
        """ Flow of the synchronization, implemented in inherited classes """
        assert self.binding_id
        assert self.binding_record

        if not self.easypost_id:
            fields = None  # should be created with all the fields

        if self._has_to_skip():
            return

        # export the missing linked resources
        self._export_dependencies()

        # prevent other jobs to export the same record
        # will be released on commit (or rollback)
        self._lock()

        map_record = self._map_data()

        if self.easypost_id:
            record = self._update_data(map_record, fields=fields)
            if not record:
                return _('Nothing to export.')
            self._update(record)
        else:
            record = self._create_data(map_record, fields=fields)
            if not record:
                return _('Nothing to export.')
            self.easypost_id = self._create(record)
        return _(
            'Record exported on Easypost w/ ID %r' % self.easypost_id
        )


@job(default_channel='root.easypost')
@related_action(action=unwrap_binding)
def export_record(session, model_name, binding_id, fields=None):
    """ Export a record to Easypost """
    record = session.env[model_name].browse(binding_id)
    env = get_environment(session, model_name, record.backend_id.id)
    exporter = env.get_connector_unit(EasypostExporter)
    return exporter.run(binding_id, fields=fields)
