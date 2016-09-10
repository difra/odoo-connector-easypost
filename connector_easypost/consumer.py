# -*- coding: utf-8 -*-
# Copyright 2016 LasLabs Inc.
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).

from .unit.export_synchronizer import (export_record,
                                       )
# from .unit.delete_synchronizer import export_delete_record
from openerp.addons.connector.event import (on_record_create,
                                            on_record_write,
                                            )


import logging
_logger = logging.getLogger(__name__)


# @on_record_create(model_names=['easypost.easypost.address'])
# def delay_create(session, model_name, record_id, vals):
#     _logger.debug('Trigger delayed export on %s, %s', model_name, record_id)
#     record = session.env[model_name].browse(record_id)
#     bind_obj = session.env['easypost.%s' % model_name]
#     if record.company_id:
#         company_id = record.company_id
#     else:
#         company_id = session.env.ref('base.main_company')
#     backend_record = company_id.easypost_backend_id
#     bind_obj.create({
#         'odoo_id': record_id,
#         'backend_id': backend_record.id,
#     })


@on_record_create(model_names=['easypost.easypost.address'])
@on_record_write(model_names=['easypost.easypost.address'])
def immediate_export(session, model_name, record_id, vals):
    """ Create immediate export of a binding record.
    (A binding record being a ``easypost.easypost.address``,
    ``easypost.easypost.address``, ...)
    """
    _logger.debug('Trigger immediate export on %s, %s', model_name, record_id)
    if session.context.get('connector_no_export'):
        return
    fields = vals.keys()
    export_record(session, model_name, record_id, fields=fields)


@on_record_write(model_names=['easypost.address'])
def immediate_export_all_bindings(session, model_name, record_id, vals):
    """ Create immediate export of all the bindings of a record.
    In this case, it is called on records of normal models and will delay
    the export for all the bindings.
    """
    _logger.debug('Trigger immediate binding export on %s, %s',
                  model_name, record_id)
    if session.context.get('connector_no_export'):
        return
    record = session.env[model_name].browse(record_id)
    fields = vals.keys()
    for binding in record.easypost_bind_ids:
        export_record(session, binding._model._name, binding.id,
                      fields=fields)


@on_record_create(model_names=['easypost.product.packaging'])
def delay_export(session, model_name, record_id, vals):
    """ Delay a job which export a binding record.
    (A binding record being a ``easypost.easypost.address``,
    ``easypost.easypost.address``, ...)
    """
    _logger.debug('Trigger delayed export on %s, %s', model_name, record_id)
    if session.context.get('connector_no_export'):
        return
    fields = vals.keys()
    export_record.delay(session, model_name, record_id, fields=fields)


@on_record_write(model_names=['product.packaging'])
def delay_export_all_bindings(session, model_name, record_id, vals):
    """ Delay a job which export all the bindings of a record.
    In this case, it is called on records of normal models and will delay
    the export for all the bindings.
    """
    if session.context.get('connector_no_export'):
        return
    record = session.env[model_name].browse(record_id)
    fields = vals.keys()
    for binding in record.easypost_bind_ids:
        export_record.delay(session, binding._model._name, binding.id,
                            fields=fields)


@on_record_create(model_names=['stock.picking'])
def delay_create(session, model_name, record_id, vals):
    """ Create a new binding record, then trigger delayed export
    In this case, it is called on records of normal models to create
    binding record, and trigger external system export
    """
    model_obj = session.env['easypost.%s' % model_name].with_context(
        connector_no_export=True,
    )
    if not len(model_obj.search([('odoo_id', '=', record_id)])):
        model_obj.create({
            'odoo_id': record_id,
        })
    delay_export_all_bindings(session, model_name, record_id, vals)
