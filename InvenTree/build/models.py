"""
Build database model definitions
"""

# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from datetime import datetime

from django.contrib.auth.models import User
from django.utils.translation import ugettext as _
from django.core.exceptions import ValidationError

from django.urls import reverse
from django.db import models, transaction
from django.core.validators import MinValueValidator

from stock.models import StockItem


class Build(models.Model):
    """ A Build object organises the creation of new parts from the component parts.

    Attributes:
        part: The part to be built (from component BOM items)
        title: Brief title describing the build (required)
        quantity: Number of units to be built
        status: Build status code
        batch: Batch code transferred to build parts (optional)
        creation_date: Date the build was created (auto)
        completion_date: Date the build was completed
        URL: External URL for extra information
        notes: Text notes
    """

    def save(self, *args, **kwargs):
        """ Called when the Build model is saved to the database.
        
        If this is a new Build, try to allocate StockItem objects automatically.
        
        - If there is only one StockItem for a Part, use that one.
        - If there are multiple StockItem objects, leave blank and let the user decide
        """

        allocate_parts = False

        # If there is no PK yet, then this is the first time the Build has been saved
        if not self.pk:
            allocate_parts = True

        # Save this Build first
        super(Build, self).save(*args, **kwargs)

        if allocate_parts:
            for item in self.part.bom_items.all():
                part = item.sub_part
                # Number of parts required for this build
                q_req = item.quantity * self.quantity

                stock = StockItem.objects.filter(part=part)

                if len(stock) == 1:
                    stock_item = stock[0]

                    # Are there any parts available?
                    if stock_item.quantity > 0:
                        # If there are not enough parts, reduce the amount we will take
                        if stock_item.quantity < q_req:
                            q_req = stock_item.quantity

                        # Allocate parts to this build
                        build_item = BuildItem(
                            build=self,
                            stock_item=stock_item,
                            quantity=q_req)

                        build_item.save()

    def __str__(self):
        return "Build {q} x {part}".format(q=self.quantity, part=str(self.part))

    def get_absolute_url(self):
        return reverse('build-detail', kwargs={'pk': self.id})

    part = models.ForeignKey('part.Part', on_delete=models.CASCADE,
                             related_name='builds',
                             limit_choices_to={'buildable': True},
                             )
    
    title = models.CharField(max_length=100, help_text='Brief description of the build')
    
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text='Number of parts to build'
    )
    
    # Build status codes
    PENDING = 10  # Build is pending / active
    CANCELLED = 30  # Build was cancelled
    COMPLETE = 40  # Build is complete

    #: Build status codes
    BUILD_STATUS_CODES = {PENDING: _("Pending"),
                          CANCELLED: _("Cancelled"),
                          COMPLETE: _("Complete"),
                          }

    status = models.PositiveIntegerField(default=PENDING,
                                         choices=BUILD_STATUS_CODES.items(),
                                         validators=[MinValueValidator(0)])
    
    batch = models.CharField(max_length=100, blank=True, null=True,
                             help_text='Batch code for this build output')
    
    creation_date = models.DateField(auto_now=True, editable=False)
    
    completion_date = models.DateField(null=True, blank=True)

    completed_by = models.ForeignKey(User,
        on_delete=models.SET_NULL,
        blank=True, null=True,
        related_name='builds_completed'
    )

    
    URL = models.URLField(blank=True, help_text='Link to external URL')

    notes = models.TextField(blank=True)
    """ Notes attached to each build output """

    @transaction.atomic
    def cancelBuild(self, user):
        """ Mark the Build as CANCELLED

        - Delete any pending BuildItem objects (but do not remove items from stock)
        - Set build status to CANCELLED
        - Save the Build object
        """

        for item in self.allocated_stock.all():
            item.delete()


        # Date of 'completion' is the date the build was cancelled
        self.completion_date = datetime.now().date()
        self.completed_by = user

        self.status = self.CANCELLED
        self.save()

    @transaction.atomic
    def completeBuild(self, location, user):
        """ Mark the Build as COMPLETE

        - Takes allocated items from stock
        - Delete pending BuildItem objects
        """

        for item in self.allocated_stock.all():
            
            # Subtract stock from the item
            item.stock_item.take_stock(
                item.quantity,
                user,
                'Removed {n} items to build {m} x {part}'.format(
                    n=item.quantity,
                    m=self.quantity,
                    part=self.part.name
                )
            )

            # Delete the item
            item.delete()

        # Mark the date of completion
        self.completion_date = datetime.now().date()

        self.completed_by = user

        # Add stock of the newly created item
        item = StockItem.objects.create(
            part=self.part,
            location=location,
            quantity=self.quantity,
            batch=str(self.batch) if self.batch else '',
            notes='Built {q} on {now}'.format(
                q=self.quantity,
                now=str(datetime.now().date())
            )
        )

        item.save()

        # Finally, mark the build as complete
        self.status = self.COMPLETE
        self.save()

    @property
    def required_parts(self):
        """ Returns a dict of parts required to build this part (BOM) """
        parts = []

        for item in self.part.bom_items.all():
            part = {'part': item.sub_part,
                    'per_build': item.quantity,
                    'quantity': item.quantity * self.quantity
                    }

            parts.append(part)

        return parts

    @property
    def can_build(self):
        """ Return true if there are enough parts to supply build """

        for item in self.required_parts:
            if item['part'].total_stock < item['quantity']:
                return False

        return True

    @property
    def is_active(self):
        """ Is this build active? An active build is either:

        - PENDING
        - HOLDING
        """

        return self.status in [
            self.PENDING,
        ]

    @property
    def is_complete(self):
        """ Returns True if the build status is COMPLETE """
        return self.status == self.COMPLETE


class BuildItem(models.Model):
    """ A BuildItem links multiple StockItem objects to a Build.
    These are used to allocate part stock to a build.
    Once the Build is completed, the parts are removed from stock and the
    BuildItemAllocation objects are removed.

    Attributes:
        build: Link to a Build object
        stock: Link to a StockItem object
        quantity: Number of units allocated
    """

    def get_absolute_url(self):
        # TODO - Fix!
        return '/build/item/{pk}/'.format(pk=self.id)
        # return reverse('build-detail', kwargs={'pk': self.id})

    class Meta:
        unique_together = [
            ('build', 'stock_item'),
        ]

    def clean(self):
        """ Check validity of the BuildItem model.
        The following checks are performed:

        - StockItem.part must be in the BOM of the Part object referenced by Build
        - Allocation quantity cannot exceed available quantity
        """
        
        super(BuildItem, self).clean()

        errors = {}

        if self.stock_item is not None and self.stock_item.part is not None:
            if self.stock_item.part not in self.build.part.required_parts():
                errors['stock_item'] = [_("Selected stock item not found in BOM for part '{p}'".format(p=self.build.part.name))]
            
        if self.stock_item is not None and self.quantity > self.stock_item.quantity:
            errors['quantity'] = [_("Allocated quantity ({n}) must not exceed available quantity ({q})".format(
                n=self.quantity,
                q=self.stock_item.quantity
            ))]

        if len(errors) > 0:
            raise ValidationError(errors)

    build = models.ForeignKey(
        Build,
        on_delete=models.CASCADE,
        related_name='allocated_stock',
        help_text='Build to allocate parts'
    )

    stock_item = models.ForeignKey(
        'stock.StockItem',
        on_delete=models.CASCADE,
        related_name='allocations',
        help_text='Stock Item to allocate to build',
    )

    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text='Stock quantity to allocate to build'
    )
